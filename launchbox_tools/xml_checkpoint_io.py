from __future__ import annotations

import codecs
import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.parsers import expat

from .operation_lifecycle import OperationControl


IO_CHUNK_SIZE = 1024 * 1024
XML_CHECKPOINT_INTERVAL = 256
_PROFILE_PREFIX_LIMIT = IO_CHUNK_SIZE
_XML_NAMESPACE_URI = "http://www.w3.org/XML/1998/namespace"
_XMLNS_NAMESPACE_URI = "http://www.w3.org/2000/xmlns/"

XmlNameIdentity = tuple[str, str, str]


@dataclass(frozen=True)
class XmlTopLevelItem:
    kind: Literal["text", "comment", "pi"]
    text: str
    target: str | None = None


@dataclass(frozen=True)
class XmlSourceProfile:
    declaration: str | None
    encoding: str
    bom: bytes
    newline: Literal["\n", "\r\n", "\r"]
    preamble: tuple[XmlTopLevelItem, ...]
    epilogue: tuple[XmlTopLevelItem, ...]


class PreservingElement(ET.Element):
    """Element retaining namespace identity without changing lexical XML names."""

    __slots__ = ("expanded_name", "expanded_attributes")

    expanded_name: tuple[str, str]
    expanded_attributes: tuple[tuple[str, str, str], ...]

    def __init__(
        self,
        tag: str,
        attrib: dict[str, str] | None = None,
        *,
        expanded_name: tuple[str, str],
        expanded_attributes: tuple[tuple[str, str, str], ...] = (),
    ) -> None:
        super().__init__(tag, attrib or {})
        self.expanded_name = expanded_name
        self.expanded_attributes = expanded_attributes

    def __deepcopy__(self, memo: dict[int, object]) -> PreservingElement:
        duplicate = type(self)(
            self.tag,
            self.attrib.copy(),
            expanded_name=self.expanded_name,
            expanded_attributes=self.expanded_attributes,
        )
        memo[id(self)] = duplicate
        duplicate.text = self.text
        duplicate.tail = self.tail
        for child in self:
            duplicate.append(copy.deepcopy(child, memo))
        return duplicate

    def __copy__(self) -> PreservingElement:
        duplicate = type(self)(
            self.tag,
            self.attrib.copy(),
            expanded_name=self.expanded_name,
            expanded_attributes=self.expanded_attributes,
        )
        duplicate.text = self.text
        duplicate.tail = self.tail
        duplicate.extend(self)
        return duplicate


def _fallback_expanded_name(name: str) -> tuple[str, str]:
    if name.startswith("{") and "}" in name:
        namespace, local = name[1:].rsplit("}", 1)
        return namespace, local
    if ":" in name:
        # A raw prefixed name without preserving-parser metadata has unknown
        # scope. Keeping the colon in the local component prevents it from
        # comparing equal to a proven unqualified name.
        return "", name
    return "", name


def xml_element_name_identity(element: ET.Element) -> XmlNameIdentity:
    if element.tag is ET.Comment:
        return "#comment", "", "#comment"
    if element.tag is ET.ProcessingInstruction:
        return "#processing-instruction", "", "#processing-instruction"
    if not isinstance(element.tag, str):
        return "", "", ""
    expanded = getattr(element, "expanded_name", None)
    if expanded is None:
        expanded = _fallback_expanded_name(element.tag)
    return element.tag, expanded[0], expanded[1]


def xml_attribute_identities(
    element: ET.Element,
) -> tuple[tuple[XmlNameIdentity, str], ...]:
    expanded_by_name = {
        raw_name: (namespace, local)
        for raw_name, namespace, local in getattr(element, "expanded_attributes", ())
    }
    identities: list[tuple[XmlNameIdentity, str]] = []
    for raw_name, value in element.attrib.items():
        if raw_name == "xmlns":
            expanded = (_XMLNS_NAMESPACE_URI, "")
        elif raw_name.startswith("xmlns:"):
            expanded = (_XMLNS_NAMESPACE_URI, raw_name.split(":", 1)[1])
        else:
            expanded = expanded_by_name.get(raw_name)
            if expanded is None:
                expanded = _fallback_expanded_name(raw_name)
        identities.append(((raw_name, expanded[0], expanded[1]), value))
    return tuple(identities)


class PreservingElementTree(ET.ElementTree):
    """ElementTree carrying the immutable lexical profile of its source XML."""

    source_profile: XmlSourceProfile

    def __init__(self, element: ET.Element, source_profile: XmlSourceProfile) -> None:
        super().__init__(element)
        self.source_profile = source_profile


class _StreamingNewlineDetector:
    def __init__(self, encoding: str, bom: bytes) -> None:
        self.decoder = codecs.getincrementaldecoder(encoding)()
        self.bom = bom
        self.first_chunk = True
        self.newline: Literal["\n", "\r\n", "\r"] | None = None
        self.pending_cr = False

    def feed(self, payload: bytes) -> None:
        if self.newline is not None:
            return
        if self.first_chunk:
            payload = payload[len(self.bom) :]
            self.first_chunk = False
        text = self.decoder.decode(payload, final=False)
        self._inspect_text(text)

    def _inspect_text(self, text: str) -> None:
        if self.pending_cr:
            if not text:
                return
            self.newline = "\r\n" if text.startswith("\n") else "\r"
            self.pending_cr = False
            return
        carriage_return = text.find("\r")
        line_feed = text.find("\n")
        indexes = [index for index in (carriage_return, line_feed) if index >= 0]
        if not indexes:
            return
        index = min(indexes)
        if text[index] == "\n":
            self.newline = "\n"
        elif index + 1 < len(text):
            self.newline = "\r\n" if text[index + 1] == "\n" else "\r"
        else:
            self.pending_cr = True

    def finish(self) -> None:
        if self.newline is not None:
            return
        remaining = self.decoder.decode(b"", final=True)
        self._inspect_text(remaining)
        if self.newline is None and self.pending_cr:
            self.newline = "\r"
            self.pending_cr = False


def _append_top_level_item(
    items: list[XmlTopLevelItem],
    kind: Literal["text", "comment", "pi"],
    text: str,
    target: str | None = None,
) -> None:
    if kind == "text" and items and items[-1].kind == "text":
        previous = items[-1]
        items[-1] = XmlTopLevelItem("text", previous.text + text)
        return
    items.append(XmlTopLevelItem(kind, text, target))


class _PreservingTreeBuilder:
    def __init__(self, control: OperationControl | None) -> None:
        self.control = control
        self.root: ET.Element | None = None
        self.stack: list[ET.Element] = []
        self.namespace_stack: list[dict[str, str]] = []
        self.root_closed = False
        self.preamble: list[XmlTopLevelItem] = []
        self.epilogue: list[XmlTopLevelItem] = []
        self.event_count = 0

    def _tick(self) -> None:
        if self.control is None:
            return
        self.event_count += 1
        if self.event_count % XML_CHECKPOINT_INTERVAL == 0:
            self.control.checkpoint()

    def _top_level_items(self) -> list[XmlTopLevelItem]:
        return self.epilogue if self.root_closed else self.preamble

    def _checkpoint_attributes(self, pair_index: int) -> None:
        if (
            self.control is not None
            and pair_index % XML_CHECKPOINT_INTERVAL == 0
        ):
            self.control.checkpoint()

    def _namespace_scope(self, attributes: list[str]) -> dict[str, str]:
        scope = (
            self.namespace_stack[-1].copy()
            if self.namespace_stack
            else {"xml": _XML_NAMESPACE_URI}
        )
        for pair_index, index in enumerate(range(0, len(attributes), 2), start=1):
            self._checkpoint_attributes(pair_index)
            name = attributes[index]
            value = attributes[index + 1]
            if name == "xmlns":
                prefix = ""
            elif name.startswith("xmlns:"):
                if name.count(":") != 1:
                    raise ET.ParseError(f"invalid namespace declaration: {name}")
                prefix = name.split(":", 1)[1]
                if not prefix:
                    raise ET.ParseError("namespace declaration prefix cannot be empty")
            else:
                continue
            if prefix == "xmlns" or value == _XMLNS_NAMESPACE_URI:
                raise ET.ParseError("reserved xmlns namespace cannot be rebound")
            if prefix == "xml" and value != _XML_NAMESPACE_URI:
                raise ET.ParseError("xml prefix must use its reserved namespace")
            if prefix != "xml" and value == _XML_NAMESPACE_URI:
                raise ET.ParseError("XML namespace can only use the xml prefix")
            if prefix and not value:
                raise ET.ParseError("namespace prefixes cannot be undeclared")
            scope[prefix] = value
        return scope

    def _expanded_name(
        self,
        name: str,
        scope: dict[str, str],
        *,
        attribute: bool,
    ) -> tuple[str, str]:
        if name.count(":") > 1:
            raise ET.ParseError(f"invalid namespace-qualified name: {name}")
        if ":" not in name:
            return ("" if attribute else scope.get("", ""), name)
        prefix, local = name.split(":", 1)
        namespace = scope.get(prefix)
        if not prefix or not local or namespace is None:
            raise ET.ParseError(f"unbound namespace prefix in name: {name}")
        return namespace, local

    def _validate_namespaces(
        self,
        name: str,
        attributes: list[str],
    ) -> tuple[
        dict[str, str],
        tuple[str, str],
        dict[str, str],
        tuple[tuple[str, str, str], ...],
    ]:
        scope = self._namespace_scope(attributes)
        expanded_element_name = self._expanded_name(name, scope, attribute=False)
        ordered_attributes: dict[str, str] = {}
        expanded_attribute_names: set[tuple[str, str]] = set()
        expanded_attributes: list[tuple[str, str, str]] = []
        for pair_index, index in enumerate(range(0, len(attributes), 2), start=1):
            self._checkpoint_attributes(pair_index)
            attribute_name = attributes[index]
            ordered_attributes[attribute_name] = attributes[index + 1]
            if attribute_name == "xmlns" or attribute_name.startswith("xmlns:"):
                continue
            expanded = self._expanded_name(attribute_name, scope, attribute=True)
            if expanded in expanded_attribute_names:
                raise ET.ParseError(
                    f"duplicate namespace-expanded attribute: {attribute_name}"
                )
            expanded_attribute_names.add(expanded)
            expanded_attributes.append((attribute_name, *expanded))
        return (
            scope,
            expanded_element_name,
            ordered_attributes,
            tuple(expanded_attributes),
        )

    def start(self, name: str, attributes: list[str]) -> None:
        self._tick()
        if self.root_closed:
            raise ET.ParseError("multiple document elements are not allowed")
        (
            namespace_scope,
            expanded_element_name,
            ordered_attributes,
            expanded_attributes,
        ) = self._validate_namespaces(name, attributes)
        element = PreservingElement(
            name,
            ordered_attributes,
            expanded_name=expanded_element_name,
            expanded_attributes=expanded_attributes,
        )
        if self.stack:
            self.stack[-1].append(element)
        elif self.root is None:
            self.root = element
        else:
            raise ET.ParseError("multiple document elements are not allowed")
        self.stack.append(element)
        self.namespace_stack.append(namespace_scope)

    def end(self, name: str) -> None:
        self._tick()
        if not self.stack or self.stack[-1].tag != name:
            raise ET.ParseError(f"unexpected closing element: {name}")
        self.stack.pop()
        self.namespace_stack.pop()
        if not self.stack:
            self.root_closed = True

    def data(self, text: str) -> None:
        if not text:
            return
        if not self.stack:
            _append_top_level_item(self._top_level_items(), "text", text)
            return
        parent = self.stack[-1]
        if len(parent):
            child = parent[-1]
            child.tail = (child.tail or "") + text
        else:
            parent.text = (parent.text or "") + text

    def comment(self, text: str) -> None:
        self._tick()
        if self.stack:
            self.stack[-1].append(ET.Comment(text))
            return
        _append_top_level_item(self._top_level_items(), "comment", text)

    def processing_instruction(self, target: str, data: str) -> None:
        self._tick()
        if self.stack:
            self.stack[-1].append(ET.ProcessingInstruction(target, data))
            return
        _append_top_level_item(self._top_level_items(), "pi", data, target)

    def default(self, text: str) -> None:
        # Expat reports whitespace outside the document element through the
        # default handler rather than CharacterDataHandler. DTDs are rejected,
        # so no other unmodelled construct is allowed through this path.
        if text and not self.stack:
            _append_top_level_item(self._top_level_items(), "text", text)


def _bom_and_encoding(prefix: bytes, declared_encoding: str | None) -> tuple[bytes, str]:
    candidates = (
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
    )
    for bom, encoding in candidates:
        if prefix.startswith(bom):
            return bom, encoding
    if declared_encoding:
        try:
            normalized = codecs.lookup(declared_encoding).name
        except LookupError as exc:
            raise ET.ParseError(
                f"unsupported XML source encoding: {declared_encoding}"
            ) from exc
        if normalized == "utf-16":
            raise ET.ParseError("UTF-16 XML requires a byte-order mark")
        if normalized == "utf-32":
            raise ET.ParseError("UTF-32 XML requires a byte-order mark")
        return b"", normalized
    if prefix.startswith(b"\x00\x00\x00<"):
        return b"", "utf-32-be"
    if prefix.startswith(b"<\x00\x00\x00"):
        return b"", "utf-32-le"
    if prefix.startswith(b"\x00<"):
        return b"", "utf-16-be"
    if prefix.startswith(b"<\x00"):
        return b"", "utf-16-le"
    return b"", "utf-8"


def _source_profile(
    prefix: bytes,
    declared_encoding: str | None,
    preamble: list[XmlTopLevelItem],
    epilogue: list[XmlTopLevelItem],
    detected_newline: Literal["\n", "\r\n", "\r"] | None,
) -> XmlSourceProfile:
    bom, encoding = _bom_and_encoding(prefix, declared_encoding)
    try:
        prefix_decoder = codecs.getincrementaldecoder(encoding)()
        decoded_prefix = prefix_decoder.decode(prefix[len(bom) :], final=False)
    except (LookupError, UnicodeError) as exc:
        raise ET.ParseError(f"could not decode XML source profile: {exc}") from exc
    declaration = _xml_declaration_from_prefix(decoded_prefix)
    newline: Literal["\n", "\r\n", "\r"] = detected_newline or "\n"
    return XmlSourceProfile(
        declaration=declaration,
        encoding=encoding,
        bom=bom,
        newline=newline,
        preamble=tuple(preamble),
        epilogue=tuple(epilogue),
    )


def _xml_declaration_from_prefix(decoded_prefix: str) -> str | None:
    if not decoded_prefix.startswith("<?xml"):
        return None
    declaration_start_length = len("<?xml")
    if (
        len(decoded_prefix) <= declaration_start_length
        or decoded_prefix[declaration_start_length] not in " \t\r\n"
    ):
        return None
    declaration_end = decoded_prefix.find("?>", declaration_start_length + 1)
    if declaration_end < 0:
        raise ET.ParseError(
            "XML declaration exceeds the safe round-trip profile limit "
            f"of {_PROFILE_PREFIX_LIMIT} bytes"
        )
    return decoded_prefix[: declaration_end + len("?>")]


def _as_element_tree_parse_error(exc: expat.ExpatError) -> ET.ParseError:
    error = ET.ParseError(str(exc))
    error.code = exc.code
    error.position = (exc.lineno, exc.offset)
    return error


def parse_xml_tree_preserving(
    path: Path,
    *,
    control: OperationControl | None = None,
) -> PreservingElementTree:
    if control is not None:
        control.checkpoint()
    builder = _PreservingTreeBuilder(control)
    parser = expat.ParserCreate()
    parser.ordered_attributes = True
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.CommentHandler = builder.comment
    parser.ProcessingInstructionHandler = builder.processing_instruction
    parser.DefaultHandler = builder.default
    declared_encoding: str | None = None

    def remember_declaration(
        _version: str,
        encoding: str | None,
        _standalone: int,
    ) -> None:
        nonlocal declared_encoding
        declared_encoding = encoding

    def reject_doctype(*_args: object) -> None:
        raise ET.ParseError("DOCTYPE is not supported by the safe XML round-trip codec")

    parser.XmlDeclHandler = remember_declaration
    parser.StartDoctypeDeclHandler = reject_doctype
    prefix = bytearray()
    newline_detector: _StreamingNewlineDetector | None = None
    try:
        with path.open("rb") as source:
            while True:
                if control is not None:
                    control.checkpoint()
                payload = source.read(IO_CHUNK_SIZE)
                if not payload:
                    break
                if len(prefix) < _PROFILE_PREFIX_LIMIT:
                    remaining = _PROFILE_PREFIX_LIMIT - len(prefix)
                    prefix.extend(payload[:remaining])
                parser.Parse(payload, False)
                if newline_detector is None:
                    bom, encoding = _bom_and_encoding(bytes(prefix), declared_encoding)
                    newline_detector = _StreamingNewlineDetector(encoding, bom)
                newline_detector.feed(payload)
                if control is not None:
                    control.checkpoint()
            parser.Parse(b"", True)
            if newline_detector is not None:
                newline_detector.finish()
    except expat.ExpatError as exc:
        raise _as_element_tree_parse_error(exc) from exc
    if builder.root is None:
        raise ET.ParseError("XML document has no document element")
    if control is not None:
        control.checkpoint()
    profile = _source_profile(
        bytes(prefix),
        declared_encoding,
        builder.preamble,
        builder.epilogue,
        newline_detector.newline if newline_detector is not None else None,
    )
    if control is not None:
        control.checkpoint()
    return PreservingElementTree(builder.root, profile)


def parse_xml_tree_with_checkpoints(
    path: Path,
    control: OperationControl,
) -> ET.ElementTree:
    return parse_xml_tree_preserving(path, control=control)

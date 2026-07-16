import codecs
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from launchbox_tools.models import MutationOutcome
from launchbox_tools.operation_lifecycle import OperationCancelled, OperationControl
from launchbox_tools.operations.dedupe_additional_apps import run_additional_apps_dedupe
from launchbox_tools.operations.path_replacement import run_path_replacement
from launchbox_tools.safe_write import XmlMutation, _serialize_xml_tree, execute_xml_transaction
from launchbox_tools.xml_checkpoint_io import PreservingElementTree
from launchbox_tools.xml_repository import local_name, parse_xml_tree

from test.support import CancelAfterCheckpoints, LaunchBoxTestCase


class XmlRoundTripTests(LaunchBoxTestCase):
    def _utf8_profile_fixture(self) -> bytes:
        document = (
            '<?xml version="1.0" encoding="utf-8"?>\r\n'
            '<?before keep?>\r\n'
            '<a:Root xmlns:a="urn:shared" xmlns:b="urn:shared" data="Привет">\r\n'
            '  <!--inside-->\r\n'
            '  <?inside value?>\r\n'
            '  <a:Child b:flag="yes">текст</a:Child>\r\n'
            '  <b:Empty />\r\n'
            '  <Scope xmlns:a="urn:other"><a:Value>scoped</a:Value></Scope>\r\n'
            '</a:Root>\r\n'
            '<!--after-->\r\n'
        )
        return codecs.BOM_UTF8 + document.encode("utf-8")

    def test_codec_preserves_profile_comments_pi_and_scoped_prefixes_exactly(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "profile.xml"
            source = self._utf8_profile_fixture()
            xml_path.write_bytes(source)

            tree = parse_xml_tree(xml_path)
            payload = _serialize_xml_tree(tree, control=OperationControl())

            self.assertIsInstance(tree, PreservingElementTree)
            self.assertEqual(tree.source_profile.declaration, '<?xml version="1.0" encoding="utf-8"?>')
            self.assertEqual(tree.source_profile.bom, codecs.BOM_UTF8)
            self.assertEqual(tree.source_profile.newline, "\r\n")
            self.assertEqual(tree.getroot().tag, "a:Root")
            self.assertEqual(
                list(tree.getroot().attrib),
                ["xmlns:a", "xmlns:b", "data"],
            )
            self.assertEqual(payload, source)

    def test_codec_does_not_add_declaration_or_bom_and_keeps_lf(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "plain.xml"
            source = b"<Root>\n  <!--keep-->\n  <Value>text</Value>\n</Root>\n"
            xml_path.write_bytes(source)

            tree = parse_xml_tree(xml_path)

            self.assertIsNone(tree.source_profile.declaration)
            self.assertEqual(tree.source_profile.bom, b"")
            self.assertEqual(tree.source_profile.newline, "\n")
            self.assertEqual(_serialize_xml_tree(tree), source)

    def test_codec_detects_crlf_after_profile_prefix_across_read_boundary(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "late-eol.xml"
            source = (
                b"<Root>"
                + b"x" * (2 * 1024 * 1024 - len(b"<Root>") - 1)
                + b"\r\n  <Child />\r\n</Root>\r\n"
            )
            xml_path.write_bytes(source)

            tree = parse_xml_tree(xml_path)

            self.assertEqual(tree.source_profile.newline, "\r\n")
            self.assertEqual(_serialize_xml_tree(tree), source)

    def test_codec_profile_prefix_may_end_inside_utf8_character(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "split-unicode.xml"
            source = (
                b"<Root>"
                + b"x" * (1024 * 1024 - len(b"<Root>") - 1)
                + "я".encode("utf-8")
                + b"</Root>"
            )
            xml_path.write_bytes(source)

            tree = parse_xml_tree(xml_path)

            self.assertEqual(_serialize_xml_tree(tree), source)

    def test_codec_preserves_cr_eol_and_exact_standalone_declaration(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "cr.xml"
            source = (
                b"<?xml version='1.0' encoding='utf-8' standalone='yes'?>\r"
                b"<Root>\r  <Value>text</Value>\r</Root>\r"
            )
            xml_path.write_bytes(source)

            tree = parse_xml_tree(xml_path)

            self.assertEqual(
                tree.source_profile.declaration,
                "<?xml version='1.0' encoding='utf-8' standalone='yes'?>",
            )
            self.assertEqual(tree.source_profile.newline, "\r")
            self.assertEqual(_serialize_xml_tree(tree), source)

    def test_codec_preserves_utf16_declaration_bom_unicode_and_crlf(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "utf16.xml"
            text = (
                '<?xml version="1.0" encoding="utf-16"?>\r\n'
                '<Root note="Привет">\r\n'
                '  <Value>世界</Value>\r\n'
                '</Root>\r\n'
            )
            source = text.encode("utf-16")
            xml_path.write_bytes(source)

            tree = parse_xml_tree(xml_path)

            self.assertIn(tree.source_profile.encoding, {"utf-16-le", "utf-16-be"})
            self.assertIn(tree.source_profile.bom, {codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE})
            self.assertEqual(_serialize_xml_tree(tree), source)

    def test_utf16_profiled_transaction_preserves_format_on_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "Utf16.xml"
            source_text = (
                '<?xml version="1.0" encoding="utf-16"?>\r\n'
                '<Root note="Привет">\r\n'
                '  <Value>old</Value>\r\n'
                '</Root>\r\n'
            )
            destination.write_bytes(source_text.encode("utf-16"))
            tree = parse_xml_tree(destination)
            next(child for child in tree.getroot() if local_name(child.tag) == "Value").text = "новое"

            with patch(
                "launchbox_tools.runtime_checks.is_launchbox_process_running",
                return_value=False,
            ):
                transaction = execute_xml_transaction(
                    [
                        XmlMutation(
                            destination,
                            tree,
                            trusted_parent=destination.parent,
                            trust_anchor=root,
                        )
                    ],
                    root / "Data" / "Backups" / "Utf16",
                )

            self.assertEqual(transaction.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(
                destination.read_bytes(),
                source_text.replace("old", "новое").encode("utf-16"),
            )

    def test_codec_rejects_doctype_instead_of_silently_dropping_it(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "doctype.xml"
            xml_path.write_text(
                '<!DOCTYPE Root [<!ENTITY value "kept">]><Root>&value;</Root>',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ET.ParseError, "DOCTYPE is not supported"):
                parse_xml_tree(xml_path)

    def test_codec_validates_namespace_bindings_while_preserving_prefixes(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "invalid-namespace.xml"
            xml_path.write_text("<missing:Root />", encoding="utf-8")

            with self.assertRaisesRegex(ET.ParseError, "unbound namespace prefix"):
                parse_xml_tree(xml_path)

            xml_path.write_text(
                '<Root xmlns:a="urn:same" xmlns:b="urn:same" a:value="1" b:value="2" />',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ET.ParseError, "duplicate namespace-expanded attribute"):
                parse_xml_tree(xml_path)

    def test_profiled_serialization_remains_cancellable_for_large_namespaced_xml(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "large.xml"
            body = "".join(f"  <p:Item>{index}</p:Item>\n" for index in range(600))
            xml_path.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<p:Root xmlns:p="urn:items">\n'
                f"{body}"
                '</p:Root>\n',
                encoding="utf-8",
            )
            tree = parse_xml_tree(xml_path)

            with self.assertRaises(OperationCancelled):
                _serialize_xml_tree(tree, control=CancelAfterCheckpoints(3))

    def test_profiled_transaction_cancellation_during_text_serialization_has_no_artifacts(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "Large.xml"
            destination.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<p:Root xmlns:p="urn:items">'
                + "x" * (3 * 1024 * 1024)
                + '</p:Root>\n',
                encoding="utf-8",
            )
            source = destination.read_bytes()
            tree = parse_xml_tree(destination)
            control = OperationControl()
            escape_calls = 0

            from launchbox_tools import safe_write

            real_escape = safe_write._escape_cdata_chunk

            def cancel_second_chunk(text: str) -> str:
                nonlocal escape_calls
                escape_calls += 1
                if escape_calls == 2:
                    control.request_cancel()
                return real_escape(text)

            with patch.object(
                safe_write,
                "_escape_cdata_chunk",
                side_effect=cancel_second_chunk,
            ):
                transaction = execute_xml_transaction(
                    [
                        XmlMutation(
                            destination,
                            tree,
                            trusted_parent=destination.parent,
                            trust_anchor=root,
                        )
                    ],
                    root / "Data" / "Backups" / "Large",
                    control=control,
                )

            self.assertEqual(transaction.outcome, MutationOutcome.CANCELLED)
            self.assertEqual(destination.read_bytes(), source)
            self.assertFalse((root / "Data" / "Backups").exists())
            self.assertFalse(control.snapshot().commit_started)
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_unencodable_mutation_fails_before_backup_or_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "Ascii.xml"
            source = (
                '<?xml version="1.0" encoding="us-ascii"?>\n'
                '<Root><Value>old</Value></Root>\n'
            ).encode("ascii")
            destination.write_bytes(source)
            tree = parse_xml_tree(destination)
            next(child for child in tree.getroot() if local_name(child.tag) == "Value").text = "🙂"
            backup_root = root / "Data" / "Backups" / "Ascii"

            transaction = execute_xml_transaction(
                [
                    XmlMutation(
                        destination,
                        tree,
                        trusted_parent=destination.parent,
                        trust_anchor=root,
                    )
                ],
                backup_root,
            )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(destination.read_bytes(), source)
            self.assertFalse(backup_root.exists())
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_dedupe_apply_preserves_non_target_xml_and_lexical_profile(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root)
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            app = (
                '<lb:AdditionalApplication ext:flag="keep">\r\n'
                '    <lb:GameID>GAME-1</lb:GameID>\r\n'
                '    <lb:Name>Manual</lb:Name>\r\n'
                '    <!--field-comment-->\r\n'
                '    <lb:ApplicationPath>Games/NES/manual.exe</lb:ApplicationPath>\r\n'
                '    <ext:Unknown ext:mode="strict">Привет</ext:Unknown>\r\n'
                '  </lb:AdditionalApplication>'
            )
            sentinel = '<ext:Sentinel ext:value="unchanged">世界</ext:Sentinel>'
            source_text = (
                '<?xml version="1.0" encoding="utf-8"?>\r\n'
                '<?launchbox keep?>\r\n'
                '<lb:LaunchBox xmlns:lb="urn:launchbox" xmlns:ext="urn:extension">\r\n'
                '  <!--root-comment-->\r\n'
                f'  {app}\r\n'
                f'  {app}\r\n'
                f'  {sentinel}\r\n'
                '</lb:LaunchBox>\r\n'
                '<!--after-root-->\r\n'
            )
            source = codecs.BOM_UTF8 + source_text.encode("utf-8")
            expected_text = (
                '<?xml version="1.0" encoding="utf-8"?>\r\n'
                '<?launchbox keep?>\r\n'
                '<lb:LaunchBox xmlns:lb="urn:launchbox" xmlns:ext="urn:extension">\r\n'
                '  <!--root-comment-->\r\n'
                f'  {app}\r\n'
                f'  {sentinel}\r\n'
                '</lb:LaunchBox>\r\n'
                '<!--after-root-->\r\n'
            )
            xml_path.write_bytes(source)

            with patch(
                "launchbox_tools.runtime_checks.is_launchbox_process_running",
                return_value=False,
            ):
                result = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(result.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(xml_path.read_bytes(), codecs.BOM_UTF8 + expected_text.encode("utf-8"))
            reread = parse_xml_tree(xml_path)
            self.assertEqual(
                sum(1 for item in reread.getroot() if local_name(item.tag) == "AdditionalApplication"),
                1,
            )
            self.assertEqual(reread.getroot().tag, "lb:LaunchBox")

    def test_dedupe_keeps_different_prefixed_unknown_nodes_conservative(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root)
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            xml_path.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<lb:LaunchBox xmlns:lb="urn:launchbox" '
                'xmlns:a="urn:extension" xmlns:b="urn:extension">\n'
                '  <lb:AdditionalApplication><lb:GameID>GAME-1</lb:GameID>'
                '<lb:Name>Manual</lb:Name>'
                '<lb:ApplicationPath>Games/NES/manual.exe</lb:ApplicationPath>'
                '<a:Unknown>same</a:Unknown></lb:AdditionalApplication>\n'
                '  <lb:AdditionalApplication><lb:GameID>GAME-1</lb:GameID>'
                '<lb:Name>Manual</lb:Name>'
                '<lb:ApplicationPath>Games/NES/manual.exe</lb:ApplicationPath>'
                '<b:Unknown>same</b:Unknown></lb:AdditionalApplication>\n'
                '</lb:LaunchBox>\n',
                encoding="utf-8",
            )
            before = xml_path.read_bytes()

            with patch(
                "launchbox_tools.runtime_checks.is_launchbox_process_running",
                return_value=False,
            ):
                result = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(result.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(len(result.results[0].duplicates), 0)
            self.assertEqual(len(result.results[0].ambiguities), 1)
            self.assertEqual(xml_path.read_bytes(), before)

    def test_replace_paths_apply_changes_only_target_and_preserves_profile(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_path = root / "Data" / "Platforms.xml"
            platforms_text = (
                '<?xml version="1.0" encoding="utf-8"?>\r\n'
                '<?launchbox keep?>\r\n'
                '<lb:ArrayOfPlatform xmlns:lb="urn:launchbox" xmlns:ext="urn:extension">\r\n'
                '  <!--metadata-comment-->\r\n'
                '  <lb:Platform ext:flag="keep">\r\n'
                '    <lb:Name>Nintendo Entertainment System</lb:Name>\r\n'
                '    <lb:Folder>Games/NES</lb:Folder>\r\n'
                '    <ext:Unknown>世界</ext:Unknown>\r\n'
                '  </lb:Platform>\r\n'
                '</lb:ArrayOfPlatform>\r\n'
                '<!--after-root-->\r\n'
            )
            source = codecs.BOM_UTF8 + platforms_text.encode("utf-8")
            platforms_path.write_bytes(source)
            games_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            games_source = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<LaunchBox><Game><Title>Other</Title>'
                '<ApplicationPath>Other/game.zip</ApplicationPath></Game></LaunchBox>\n'
            ).encode("utf-8")
            games_path.write_bytes(games_source)

            with patch(
                "launchbox_tools.runtime_checks.is_launchbox_process_running",
                return_value=False,
            ):
                result = run_path_replacement(
                    root,
                    root / "Games",
                    root / "Library",
                    apply_changes=True,
                )

            self.assertEqual(result.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(
                platforms_path.read_bytes(),
                source.replace(b"Games/NES", b"Library/NES", 1),
            )
            self.assertEqual(games_path.read_bytes(), games_source)


if __name__ == "__main__":
    unittest.main()

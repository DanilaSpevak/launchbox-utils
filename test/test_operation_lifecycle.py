import threading
import unittest

from launchbox_tools.operation_lifecycle import (
    OperationCancelled,
    OperationControl,
    OperationPhase,
)


class OperationLifecycleTests(unittest.TestCase):
    def test_cancel_before_commit_prevents_commit(self) -> None:
        control = OperationControl()
        control.set_phase(OperationPhase.STAGE)

        self.assertTrue(control.request_cancel())
        with self.assertRaises(OperationCancelled):
            control.begin_commit()

        snapshot = control.snapshot()
        self.assertEqual(snapshot.phase, OperationPhase.STAGE)
        self.assertTrue(snapshot.cancel_requested)
        self.assertFalse(snapshot.commit_started)

    def test_commit_start_permanently_rejects_cancel(self) -> None:
        control = OperationControl()

        control.begin_commit()
        control.set_phase(OperationPhase.ROLLBACK)

        self.assertFalse(control.request_cancel())
        snapshot = control.snapshot()
        self.assertEqual(snapshot.phase, OperationPhase.ROLLBACK)
        self.assertFalse(snapshot.cancel_requested)
        self.assertTrue(snapshot.commit_started)

    def test_cancel_and_commit_race_has_one_winner(self) -> None:
        for _ in range(50):
            control = OperationControl()
            barrier = threading.Barrier(3)
            outcomes: list[str] = []

            def cancel() -> None:
                barrier.wait()
                outcomes.append("cancelled" if control.request_cancel() else "cancel_rejected")

            def commit() -> None:
                barrier.wait()
                try:
                    control.begin_commit()
                    outcomes.append("commit_started")
                except OperationCancelled:
                    outcomes.append("commit_cancelled")

            cancel_thread = threading.Thread(target=cancel)
            commit_thread = threading.Thread(target=commit)
            cancel_thread.start()
            commit_thread.start()
            barrier.wait()
            cancel_thread.join(timeout=2)
            commit_thread.join(timeout=2)

            self.assertIn(
                sorted(outcomes),
                [
                    ["cancel_rejected", "commit_started"],
                    ["cancelled", "commit_cancelled"],
                ],
            )

    def test_finish_updates_phase_without_unlocking_commit(self) -> None:
        control = OperationControl()
        control.begin_commit()

        control.finish()

        self.assertEqual(control.snapshot().phase, OperationPhase.FINISHED)
        self.assertFalse(control.request_cancel())

    def test_finalize_rejects_cancel_without_commit(self) -> None:
        control = OperationControl()
        control.set_phase(OperationPhase.FINALIZE)

        self.assertFalse(control.request_cancel())
        self.assertFalse(control.snapshot().cancel_requested)

    def test_begin_finalize_honors_pending_cancel_and_closes_phase(self) -> None:
        control = OperationControl()
        self.assertTrue(control.request_cancel())

        with self.assertRaises(OperationCancelled):
            control.begin_finalize()

        snapshot = control.snapshot()
        self.assertEqual(snapshot.phase, OperationPhase.FINALIZE)
        self.assertTrue(snapshot.cancel_requested)
        self.assertFalse(control.request_cancel())

        control.set_phase(OperationPhase.SCAN)
        self.assertFalse(control.request_cancel())

    def test_cancel_and_finalize_race_has_one_winner(self) -> None:
        for _ in range(50):
            control = OperationControl()
            barrier = threading.Barrier(3)
            outcomes: list[str] = []

            def cancel() -> None:
                barrier.wait()
                outcomes.append("cancelled" if control.request_cancel() else "cancel_rejected")

            def finalize() -> None:
                barrier.wait()
                try:
                    control.begin_finalize()
                    outcomes.append("finalize_started")
                except OperationCancelled:
                    outcomes.append("finalize_cancelled")

            cancel_thread = threading.Thread(target=cancel)
            finalize_thread = threading.Thread(target=finalize)
            cancel_thread.start()
            finalize_thread.start()
            barrier.wait()
            cancel_thread.join(timeout=2)
            finalize_thread.join(timeout=2)

            self.assertIn(
                sorted(outcomes),
                [
                    ["cancel_rejected", "finalize_started"],
                    ["cancelled", "finalize_cancelled"],
                ],
            )
            self.assertEqual(control.snapshot().phase, OperationPhase.FINALIZE)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

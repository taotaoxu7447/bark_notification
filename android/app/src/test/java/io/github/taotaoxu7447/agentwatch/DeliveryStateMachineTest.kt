package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Test

class DeliveryStateMachineTest {
    @Test
    fun ambiguousPendingDisplayMustBeProvenOrSilentlyRecoveredBeforeAck() {
        assertEquals(
            EventDedupeStore.RecoveryAction.POST_SILENT_THEN_QUEUE_ACK,
            EventDedupeStore.recoveryAction(
                EventDedupeStore.Stage.PENDING_DISPLAY,
                notificationActive = false,
            ),
        )
        assertEquals(
            EventDedupeStore.RecoveryAction.COMMIT_ACTIVE_THEN_QUEUE_ACK,
            EventDedupeStore.recoveryAction(
                EventDedupeStore.Stage.PENDING_DISPLAY,
                notificationActive = true,
            ),
        )
    }

    @Test
    fun committedDisplayCanQueueAckWithoutPostingAgain() {
        assertEquals(
            EventDedupeStore.RecoveryAction.QUEUE_ACK,
            EventDedupeStore.recoveryAction(
                EventDedupeStore.Stage.DISPLAY_COMMITTED,
                notificationActive = false,
            ),
        )
    }

    @Test
    fun terminalStatesNeedNoRecoveryWork() {
        listOf(EventDedupeStore.Stage.SHOWN, EventDedupeStore.Stage.SUPPRESSED).forEach { stage ->
            assertEquals(
                EventDedupeStore.RecoveryAction.NONE,
                EventDedupeStore.recoveryAction(stage, notificationActive = false),
            )
        }
    }
}

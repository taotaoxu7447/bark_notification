package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue

class WatchServiceTest {
    @Test
    fun reconnectBackoffIsBounded() {
        assertEquals(1, WatchService.backoffSeconds(0))
        assertEquals(2, WatchService.backoffSeconds(1))
        assertEquals(32, WatchService.backoffSeconds(5))
        assertEquals(60, WatchService.backoffSeconds(6))
        assertEquals(60, WatchService.backoffSeconds(100))
    }

    @Test
    fun sameDefaultNetworkReconnectsWhenInternetCapabilityReturns() {
        assertEquals(
            WatchService.Companion.NetworkCapabilityAction.CONNECT,
            WatchService.networkCapabilityAction(
                isCurrentDefault = true,
                hadInternet = false,
                hasInternet = true,
            ),
        )
    }

    @Test
    fun capabilityLossDisconnectsOnlyTheCurrentDefaultNetwork() {
        assertEquals(
            WatchService.Companion.NetworkCapabilityAction.DISCONNECT,
            WatchService.networkCapabilityAction(
                isCurrentDefault = true,
                hadInternet = true,
                hasInternet = false,
            ),
        )
        assertEquals(
            WatchService.Companion.NetworkCapabilityAction.NONE,
            WatchService.networkCapabilityAction(
                isCurrentDefault = false,
                hadInternet = true,
                hasInternet = false,
            ),
        )
    }

    @Test
    fun deletedHistoryTombstoneSuppressesReplayEvenAfterDedupeTrim() {
        assertTrue(WatchService.shouldSuppressFromHistory(HistoryStore.InsertResult.DELETED))
        assertFalse(WatchService.shouldSuppressFromHistory(HistoryStore.InsertResult.EXISTS))
    }

    @Test
    fun replayDoesNotResurrectDeletedHistoryButFirstSuppressedDeliveryIsKept() {
        assertTrue(
            WatchService.shouldRemoveUnexpectedInsert(
                EventDedupeStore.Claim.SHOWN,
                HistoryStore.InsertResult.INSERTED,
            ),
        )
        assertTrue(
            WatchService.shouldRemoveUnexpectedInsert(
                EventDedupeStore.Claim.DISPLAY_COMMITTED,
                HistoryStore.InsertResult.INSERTED,
            ),
        )
        assertFalse(
            WatchService.shouldRemoveUnexpectedInsert(
                EventDedupeStore.Claim.SUPPRESSED,
                HistoryStore.InsertResult.INSERTED,
            ),
        )
    }
}

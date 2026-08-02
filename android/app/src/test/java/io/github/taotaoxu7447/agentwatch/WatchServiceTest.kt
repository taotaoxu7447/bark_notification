package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Test

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
}

package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AppConfigTest {
    @Test
    fun firstConnectionDoesNotRequestCachedMessages() {
        val url = AppConfig.websocketUrl(CursorStore.Cursor(serverEpochSeconds = 0L))
        assertTrue(url.startsWith("wss://"))
        assertFalse(url.contains("since="))
    }

    @Test
    fun reconnectUsesServerTimeInsteadOfPotentiallyUncachedMessageId() {
        val url = AppConfig.websocketUrl(CursorStore.Cursor(serverEpochSeconds = 1_785_600_123L))
        assertEquals("1785600123", url.substringAfter("since="))
    }
}

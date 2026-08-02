package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AppConfigTest {
    @Test
    fun firstConnectionDoesNotRequestCachedMessages() {
        val url = AppConfig.websocketUrl(
            "wss://64.90.8.184:9444/aw-0123456789abcdef0123456789abcdef/ws",
            CursorStore.Cursor(serverEpochSeconds = 0L),
        )
        assertTrue(url.startsWith("wss://"))
        assertFalse(url.contains("since="))
    }

    @Test
    fun reconnectUsesServerTimeInsteadOfPotentiallyUncachedMessageId() {
        val url = AppConfig.websocketUrl(
            "wss://64.90.8.184:9444/aw-0123456789abcdef0123456789abcdef/ws",
            CursorStore.Cursor(serverEpochSeconds = 1_785_600_123L),
        )
        assertEquals("1785600123", url.substringAfter("since="))
    }

    @Test
    fun privateSessionMustStayOnConfiguredRelayAndTopicPath() {
        assertTrue(
            AppConfig.validPrivateSession(
                "aw-0123456789abcdef0123456789abcdef",
                "https://64.90.8.184:9444/aw-0123456789abcdef0123456789abcdef",
                "wss://64.90.8.184:9444/aw-0123456789abcdef0123456789abcdef/ws",
            ),
        )
        assertFalse(
            AppConfig.validPrivateSession(
                "aw-0123456789abcdef0123456789abcdef",
                "https://64.90.8.184:9444/aw-0123456789abcdef0123456789abcdef",
                "wss://attacker.example/aw-0123456789abcdef0123456789abcdef/ws",
            ),
        )
    }
}

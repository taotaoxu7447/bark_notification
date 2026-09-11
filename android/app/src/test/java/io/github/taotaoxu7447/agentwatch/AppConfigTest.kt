package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AppConfigTest {
    @Test
    fun firstConnectionDoesNotRequestCachedMessages() {
        val url = AppConfig.websocketUrl(
            "wss://191.222.219.94:9444/aw-0123456789abcdef0123456789abcdef/ws",
            CursorStore.Cursor(serverEpochSeconds = 0L),
        )
        assertTrue(url.startsWith("wss://"))
        assertFalse(url.contains("since="))
    }

    @Test
    fun reconnectUsesServerTimeInsteadOfPotentiallyUncachedMessageId() {
        val url = AppConfig.websocketUrl(
            "wss://191.222.219.94:9444/aw-0123456789abcdef0123456789abcdef/ws",
            CursorStore.Cursor(serverEpochSeconds = 1_785_600_123L),
        )
        assertEquals("1785600123", url.substringAfter("since="))
    }

    @Test
    fun privateSessionMustStayOnConfiguredRelayAndTopicPath() {
        assertTrue(
            AppConfig.validPrivateSession(
                "aw-0123456789abcdef0123456789abcdef",
                "https://aw.taotaoxu.net/aw-0123456789abcdef0123456789abcdef",
                "wss://aw.taotaoxu.net/aw-0123456789abcdef0123456789abcdef/ws",
            ),
        )
        assertFalse(
            AppConfig.validPrivateSession(
                "aw-0123456789abcdef0123456789abcdef",
                "https://aw.taotaoxu.net/aw-0123456789abcdef0123456789abcdef",
                "wss://attacker.example/aw-0123456789abcdef0123456789abcdef/ws",
            ),
        )
    }

    @Test
    fun legacySessionIsAcceptedOnlyByLegacyValidator() {
        val topic = "aw-0123456789abcdef0123456789abcdef"
        val publish = "https://191.222.219.94:9444/$topic"
        val websocket = "wss://191.222.219.94:9444/$topic/ws"

        assertTrue(AppConfig.validLegacyPrivateSession(topic, publish, websocket))
        assertFalse(AppConfig.validPrivateSession(topic, publish, websocket))
    }

    @Test
    fun currentUrlsUseImplicitHttpsPort443() {
        val topic = "aw-0123456789abcdef0123456789abcdef"

        assertEquals("https://aw.taotaoxu.net/$topic", AppConfig.currentPublishUrl(topic))
        assertEquals("wss://aw.taotaoxu.net/$topic/ws", AppConfig.currentWebsocketUrl(topic))
    }
}

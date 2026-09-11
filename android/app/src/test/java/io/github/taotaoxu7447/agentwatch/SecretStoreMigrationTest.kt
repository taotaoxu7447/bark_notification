package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test

class SecretStoreMigrationTest {
    @Test
    fun v01AppTokenAndLegacyUsernameTriggerUpgradeWithoutPassword() {
        val legacy = SecretStore.Session(
            username = "",
            ntfyToken = "tk_old",
            appToken = "legacy_app_token_abcdefghijklmnopqrstuvwxyz",
            ntfyTopic = "",
            ntfyUrl = "",
            ntfyWebsocketUrl = "",
        )
        assertTrue(SecretStore.legacyUpgradeRequired(legacy, "owner"))
        assertFalse(SecretStore.legacyUpgradeRequired(legacy, ""))
    }

    @Test
    fun retiredServerPrivateSessionRequiresAddressUpgrade() {
        val retired = SecretStore.Session(
            username = "alice",
            ntfyToken = "tk_123456789",
            appToken = "abcdefghijklmnopqrstuvwxyz123456",
            ntfyTopic = "aw-0123456789abcdef0123456789abcdef",
            ntfyUrl = "https://191.222.219.94:9444/aw-0123456789abcdef0123456789abcdef",
            ntfyWebsocketUrl = "wss://191.222.219.94:9444/aw-0123456789abcdef0123456789abcdef/ws",
        )

        assertFalse(retired.isPrivate)
        assertTrue(SecretStore.legacyUpgradeRequired(retired, "alice"))

        val migrated = SecretStore.migratedLegacySession(retired)
        assertEquals("https://aw.taotaoxu.net/${retired.ntfyTopic}", migrated?.ntfyUrl)
        assertEquals("wss://aw.taotaoxu.net/${retired.ntfyTopic}/ws", migrated?.ntfyWebsocketUrl)
        assertEquals(retired.username, migrated?.username)
        assertEquals(retired.ntfyToken, migrated?.ntfyToken)
        assertEquals(retired.appToken, migrated?.appToken)
    }
}

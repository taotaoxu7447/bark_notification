package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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
}

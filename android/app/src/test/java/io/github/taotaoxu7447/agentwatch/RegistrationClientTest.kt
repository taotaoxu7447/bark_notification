package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import org.json.JSONObject

class RegistrationClientTest {
    @Test
    fun acknowledgementAuthFailuresAreTerminal() {
        listOf(401, 403).forEach { status ->
            val error = RegistrationClient.HttpStatusException(status, "rejected")
            assertEquals(
                RegistrationClient.AcknowledgeResult.AUTH_REJECTED,
                RegistrationClient.classifyAcknowledgement(error),
            )
            assertTrue(RegistrationClient.remoteSessionIsAbsent(error))
        }
    }

    @Test
    fun acknowledgementTransientFailuresRemainRetryable() {
        assertEquals(
            RegistrationClient.AcknowledgeResult.RETRYABLE_FAILURE,
            RegistrationClient.classifyAcknowledgement(IOException("offline")),
        )
        assertEquals(
            RegistrationClient.AcknowledgeResult.RETRYABLE_FAILURE,
            RegistrationClient.classifyAcknowledgement(
                RegistrationClient.HttpStatusException(503, "unavailable"),
            ),
        )
        assertFalse(
            RegistrationClient.remoteSessionIsAbsent(
                RegistrationClient.HttpStatusException(429, "limited"),
            ),
        )
    }

    @Test
    fun successfulAcknowledgementIsRecognized() {
        assertEquals(
            RegistrationClient.AcknowledgeResult.ACKNOWLEDGED,
            RegistrationClient.classifyAcknowledgement(null),
        )
    }

    @Test
    fun logoutTreatsMissingRemoteSessionAsCompleted() {
        listOf(401, 403).forEach { status ->
            assertTrue(
                RegistrationClient.normalizeLogoutResult(
                    RegistrationClient.HttpStatusException(status, "already gone"),
                ).isSuccess,
            )
        }
        assertTrue(RegistrationClient.normalizeLogoutResult(null).isSuccess)
        assertTrue(
            RegistrationClient.normalizeLogoutResult(
                RegistrationClient.HttpStatusException(503, "retry"),
            ).isFailure,
        )
    }

    @Test
    fun v2AuthResponseRequiresPrivateDynamicChannel() {
        val session = RegistrationClient.parseAuthSession(
            JSONObject()
                .put("api_version", 2)
                .put("username", "owner")
                .put("ntfy_token", "tk_123456789")
                .put("app_token", "abcdefghijklmnopqrstuvwxyz123456")
                .put("ntfy_topic", "aw-0123456789abcdef0123456789abcdef")
                .put("ntfy_url", "https://64.90.8.184:9444/aw-0123456789abcdef0123456789abcdef")
                .put("ntfy_ws_url", "wss://64.90.8.184:9444/aw-0123456789abcdef0123456789abcdef/ws"),
        )
        assertEquals("aw-0123456789abcdef0123456789abcdef", session.ntfyTopic)
    }
}

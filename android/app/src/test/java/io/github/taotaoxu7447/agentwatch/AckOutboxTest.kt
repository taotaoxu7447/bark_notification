package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Test

class AckOutboxTest {
    @Test
    fun retryBackoffStartsQuicklyAndCapsAtFiveMinutes() {
        assertEquals(5L, AckOutbox.retryDelaySeconds(1))
        assertEquals(10L, AckOutbox.retryDelaySeconds(2))
        assertEquals(160L, AckOutbox.retryDelaySeconds(6))
        assertEquals(300L, AckOutbox.retryDelaySeconds(7))
        assertEquals(300L, AckOutbox.retryDelaySeconds(100))
    }
}

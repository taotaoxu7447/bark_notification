package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HistorySettingsTest {
    @Test
    fun defaultsToSevenDaysAndPermanentDisablesOnlyAgeCutoff() {
        assertEquals(7, HistorySettings.normalizeRetentionDays(999))
        assertEquals(1_000_000L - 7L * 86_400_000L, HistorySettings.cutoffMillis(1_000_000L, 7))
        assertNull(HistorySettings.cutoffMillis(1_000_000L, HistorySettings.PERMANENT))
        assertEquals(500, HistorySettings.MAX_MESSAGES_PER_ACCOUNT)
    }

    @Test
    fun searchEscapesSqlLikeWildcards() {
        assertEquals("100\\%\\_done\\\\ok", HistoryStore.escapeLike("100%_done\\ok"))
    }
}

package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Test

class MainUiLogicTest {
    @Test
    fun `private session shows the signed-in interface`() {
        assertEquals(
            MainUiLogic.SessionVisibility(false, true, true),
            MainUiLogic.sessionVisibility(isPrivate = true, authenticationFailed = false),
        )
    }

    @Test
    fun `authentication failure never overlaps signed-in pages`() {
        assertEquals(
            MainUiLogic.SessionVisibility(true, false, false),
            MainUiLogic.sessionVisibility(isPrivate = true, authenticationFailed = true),
        )
    }

    @Test
    fun `signed-out session shows only authentication`() {
        assertEquals(
            MainUiLogic.SessionVisibility(true, false, false),
            MainUiLogic.sessionVisibility(isPrivate = false, authenticationFailed = false),
        )
    }
}

package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Test

class DeviceIdentityTest {
    @Test
    fun deviceTargetMatchesRegistrationServerContract() {
        assertEquals(
            "15ff1f3285f342871079a446",
            DeviceIdentity.notificationTargetForId("device-12345678"),
        )
    }
}

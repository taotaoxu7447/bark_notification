package io.github.taotaoxu7447.agentwatch

internal object MainUiLogic {
    data class SessionVisibility(
        val showAuthentication: Boolean,
        val showNavigation: Boolean,
        val showPages: Boolean,
    )

    fun sessionVisibility(isPrivate: Boolean, authenticationFailed: Boolean): SessionVisibility {
        val authenticated = isPrivate && !authenticationFailed
        return SessionVisibility(
            showAuthentication = !authenticated,
            showNavigation = authenticated,
            showPages = authenticated,
        )
    }
}

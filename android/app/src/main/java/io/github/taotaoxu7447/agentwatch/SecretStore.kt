package io.github.taotaoxu7447.agentwatch

import android.annotation.SuppressLint
import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.nio.ByteBuffer
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

@SuppressLint("ApplySharedPref")
class SecretStore(context: Context) {
    data class Session(
        val username: String,
        val ntfyToken: String,
        val appToken: String,
        val ntfyTopic: String,
        val ntfyUrl: String,
        val ntfyWebsocketUrl: String,
    ) {
        val isPrivate: Boolean
            get() = username.isNotBlank() && ntfyToken.isNotBlank() && appToken.isNotBlank() &&
                AppConfig.validPrivateSession(ntfyTopic, ntfyUrl, ntfyWebsocketUrl)
    }

    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun put(name: String, value: String) {
        if (value.isBlank()) {
            remove(name)
            return
        }
        // Persist credentials before starting the foreground receiver service.
        check(preferences.edit().putString(name, encrypt(value)).commit()) {
            "Could not persist encrypted credential"
        }
    }

    fun saveSession(session: Session) {
        require(session.isPrivate) { "Invalid private notification session" }
        val editor = preferences.edit()
        mapOf(
            USERNAME to session.username,
            NTFY_TOKEN to session.ntfyToken,
            APP_TOKEN to session.appToken,
            NTFY_TOPIC to session.ntfyTopic,
            NTFY_URL to session.ntfyUrl,
            NTFY_WEBSOCKET_URL to session.ntfyWebsocketUrl,
        ).forEach { (name, value) -> editor.putString(name, encrypt(value)) }
        check(editor.commit()) { "Could not persist encrypted private session" }
    }

    fun session(): Session = Session(
        username = get(USERNAME),
        ntfyToken = get(NTFY_TOKEN),
        appToken = get(APP_TOKEN),
        ntfyTopic = get(NTFY_TOPIC),
        ntfyUrl = get(NTFY_URL),
        ntfyWebsocketUrl = get(NTFY_WEBSOCKET_URL),
    )

    fun get(name: String): String {
        val encoded = preferences.getString(name, null) ?: return ""
        return try {
            val packed = ByteBuffer.wrap(Base64.decode(encoded, Base64.NO_WRAP))
            val ivLength = packed.int
            require(ivLength in 12..32)
            val iv = ByteArray(ivLength)
            packed.get(iv)
            val encrypted = ByteArray(packed.remaining())
            packed.get(encrypted)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
            String(cipher.doFinal(encrypted), Charsets.UTF_8)
        } catch (_: Exception) {
            check(preferences.edit().remove(name).commit()) { "Could not remove invalid credential" }
            ""
        }
    }

    fun remove(name: String) {
        check(preferences.edit().remove(name).commit()) { "Could not remove credential" }
    }

    fun clearSession() {
        check(preferences.edit().clear().commit()) { "Could not clear credentials" }
    }

    private fun encrypt(value: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        val packed = ByteBuffer.allocate(4 + cipher.iv.size + encrypted.size)
            .putInt(cipher.iv.size)
            .put(cipher.iv)
            .put(encrypted)
            .array()
        return Base64.encodeToString(packed, Base64.NO_WRAP)
    }

    private fun key(): SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    companion object {
        const val USERNAME = "username"
        const val NTFY_TOKEN = "ntfy_token"
        const val APP_TOKEN = "app_token"
        const val NTFY_TOPIC = "ntfy_topic"
        const val NTFY_URL = "ntfy_url"
        const val NTFY_WEBSOCKET_URL = "ntfy_websocket_url"
        private const val PREFERENCES = "agentwatch_secrets"
        private const val KEY_ALIAS = "agentwatch_session_key_v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"

        internal fun legacyUpgradeRequired(session: Session, legacyUsername: String): Boolean =
            !session.isPrivate && session.appToken.isNotBlank() && legacyUsername.isNotBlank()
    }
}

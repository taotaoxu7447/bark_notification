package io.github.taotaoxu7447.agentwatch

import android.annotation.SuppressLint
import android.content.Context
import android.os.Build
import java.security.MessageDigest
import java.util.UUID

@SuppressLint("ApplySharedPref")
object DeviceIdentity {
    private const val PREFERENCES = "agentwatch_identity"
    private const val KEY_DEVICE_ID = "device_id"
    private const val KEY_USERNAME = "username"

    fun id(context: Context): String {
        val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
        preferences.getString(KEY_DEVICE_ID, null)?.takeIf { it.isNotBlank() }?.let { return it }
        val generated = UUID.randomUUID().toString()
        check(preferences.edit().putString(KEY_DEVICE_ID, generated).commit()) {
            "Could not persist device identity"
        }
        return generated
    }

    fun defaultName(): String = listOf(Build.MANUFACTURER, Build.MODEL)
        .filter { it.isNotBlank() }
        .distinct()
        .joinToString(" ")
        .ifBlank { "Android device" }

    fun username(context: Context): String =
        context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE).getString(KEY_USERNAME, "") ?: ""

    fun notificationTarget(context: Context): String = notificationTargetForId(id(context))

    internal fun notificationTargetForId(deviceId: String): String = MessageDigest.getInstance("SHA-256")
        .digest(deviceId.toByteArray(Charsets.UTF_8))
        .take(12)
        .joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }

    fun setUsername(context: Context, username: String) {
        context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_USERNAME, username)
            .commit()
            .also { check(it) { "Could not persist username" } }
    }

    fun clearUsername(context: Context) {
        check(
            context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
                .edit()
                .remove(KEY_USERNAME)
                .commit(),
        ) { "Could not clear username" }
    }
}

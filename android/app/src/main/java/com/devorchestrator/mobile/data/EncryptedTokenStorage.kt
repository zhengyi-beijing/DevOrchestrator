package com.devorchestrator.mobile.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

interface TokenStorage {
    var gatewayBaseUrl: String?
    var deviceId: String?
    var bearerToken: String?
    fun clearCredentials()
    fun hasValidCredentials(): Boolean
}

class EncryptedTokenStorage(context: Context) : TokenStorage {
    private val prefs: SharedPreferences

    init {
        prefs = try {
            val masterKey = MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            EncryptedSharedPreferences.create(
                context,
                "devo_mobile_auth",
                masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
            )
        } catch (e: Exception) {
            // Fallback for instrumentation test environments lacking hardware keystore
            context.getSharedPreferences("devo_mobile_auth_fallback", Context.MODE_PRIVATE)
        }
    }

    override var gatewayBaseUrl: String?
        get() = prefs.getString(KEY_GATEWAY_URL, null)
        set(value) = prefs.edit().putString(KEY_GATEWAY_URL, value).apply()

    override var deviceId: String?
        get() = prefs.getString(KEY_DEVICE_ID, null)
        set(value) = prefs.edit().putString(KEY_DEVICE_ID, value).apply()

    override var bearerToken: String?
        get() = prefs.getString(KEY_BEARER_TOKEN, null)
        set(value) = prefs.edit().putString(KEY_BEARER_TOKEN, value).apply()

    override fun clearCredentials() {
        prefs.edit()
            .remove(KEY_DEVICE_ID)
            .remove(KEY_BEARER_TOKEN)
            .apply()
    }

    override fun hasValidCredentials(): Boolean {
        return !deviceId.isNullOrBlank() && !bearerToken.isNullOrBlank() && !gatewayBaseUrl.isNullOrBlank()
    }

    companion object {
        private const val KEY_GATEWAY_URL = "gateway_base_url"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_BEARER_TOKEN = "bearer_token"
    }
}

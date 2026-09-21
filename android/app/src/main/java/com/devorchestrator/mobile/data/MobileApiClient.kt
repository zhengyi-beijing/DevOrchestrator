package com.devorchestrator.mobile.data

import com.devorchestrator.mobile.model.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class MobileApiClient(
    private val tokenStorage: TokenStorage,
    private val okHttpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()
) {
    private val jsonMedia = "application/json; charset=utf-8".toMediaType()
    private var eventSource: EventSource? = null

    private fun baseUrl(): String {
        return tokenStorage.gatewayBaseUrl?.trimEnd('/') ?: throw IllegalStateException("Gateway URL not set")
    }

    private fun authHeader(): String {
        val token = tokenStorage.bearerToken ?: throw IllegalStateException("Bearer token not set")
        return "Bearer $token"
    }

    fun pair(
        gatewayUrl: String,
        pairingId: String,
        code: String,
        deviceLabel: String,
        callback: (Result<PairResponse>) -> Unit
    ) {
        val url = "${gatewayUrl.trimEnd('/')}/api/v1/mobile/v1/pair"
        val bodyObj = JSONObject().apply {
            put("pairing_id", pairingId)
            put("code", code)
            put("device_label", deviceLabel)
        }
        val request = Request.Builder()
            .url(url)
            .post(bodyObj.toString().toRequestBody(jsonMedia))
            .build()

        okHttpClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                callback(Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                response.use { resp ->
                    if (!resp.isSuccessful) {
                        callback(Result.failure(IOException("Pairing failed: HTTP ${resp.code}")))
                        return
                    }
                    val json = JSONObject(resp.body?.string() ?: "{}")
                    val pairResponse = PairResponse(
                        device_id = json.optString("device_id"),
                        token = json.optString("token"),
                        scope = json.optString("scope"),
                        expires_at = if (json.has("expires_at")) json.getString("expires_at") else null
                    )
                    tokenStorage.gatewayBaseUrl = gatewayUrl
                    tokenStorage.deviceId = pairResponse.device_id
                    tokenStorage.bearerToken = pairResponse.token
                    callback(Result.success(pairResponse))
                }
            }
        })
    }

    fun getProjects(callback: (Result<List<ProjectSummary>>) -> Unit) {
        val request = Request.Builder()
            .url("${baseUrl()}/api/v1/mobile/v1/projects")
            .header("Authorization", authHeader())
            .get()
            .build()

        okHttpClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                callback(Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                response.use { resp ->
                    if (resp.code == 401) {
                        tokenStorage.clearCredentials()
                        callback(Result.failure(SecurityException("Session revoked or expired")))
                        return
                    }
                    if (!resp.isSuccessful) {
                        callback(Result.failure(IOException("HTTP ${resp.code}")))
                        return
                    }
                    val json = JSONObject(resp.body?.string() ?: "{}")
                    val arr = json.optJSONArray("projects") ?: JSONArray()
                    val list = mutableListOf<ProjectSummary>()
                    for (i in 0 until arr.length()) {
                        val item = arr.getJSONObject(i)
                        list.add(
                            ProjectSummary(
                                project_id = item.optString("project_id"),
                                name = item.optString("name", item.optString("project_id")),
                                progress_observation_state = item.optString("progress_observation_state", "unavailable"),
                                status = item.optString("status", "unknown")
                            )
                        )
                    }
                    callback(Result.success(list))
                }
            }
        })
    }

    fun submitControl(
        projectId: String,
        action: String,
        expectedRevision: Int,
        targetId: String? = null,
        deviceRequestId: String,
        callback: (Result<ControlCommandResponse>) -> Unit
    ) {
        val url = "${baseUrl()}/api/v1/mobile/v1/projects/$projectId/controls"
        val bodyObj = JSONObject().apply {
            put("action", action)
            put("expected_revision", expectedRevision)
            put("device_request_id", deviceRequestId)
            if (targetId != null) {
                put("target_id", targetId)
            }
        }
        val request = Request.Builder()
            .url(url)
            .header("Authorization", authHeader())
            .post(bodyObj.toString().toRequestBody(jsonMedia))
            .build()

        okHttpClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                callback(Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                response.use { resp ->
                    if (resp.code == 401) {
                        tokenStorage.clearCredentials()
                        callback(Result.failure(SecurityException("Session revoked or expired")))
                        return
                    }
                    if (resp.code == 409) {
                        callback(Result.failure(IllegalStateException("Revision conflict or duplicate command")))
                        return
                    }
                    if (!resp.isSuccessful) {
                        callback(Result.failure(IOException("HTTP ${resp.code}")))
                        return
                    }
                    val json = JSONObject(resp.body?.string() ?: "{}")
                    callback(
                        Result.success(
                            ControlCommandResponse(
                                command_id = json.optString("command_id"),
                                status = json.optString("status", "submitted")
                            )
                        )
                    )
                }
            }
        })
    }

    fun startEventStream(
        lastEventId: String? = null,
        onEvent: (MobileEvent) -> Unit,
        onResyncRequired: () -> Unit,
        onTerminalUnauthorized: () -> Unit
    ) {
        stopEventStream()
        val sseClient = okHttpClient.newBuilder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build()
        val factory = EventSources.createFactory(sseClient)
        val requestBuilder = Request.Builder()
            .url("${baseUrl()}/api/v1/mobile/v1/events")
            .header("Authorization", authHeader())
            .header("Accept", "text/event-stream")

        if (!lastEventId.isNullOrBlank()) {
            requestBuilder.header("Last-Event-ID", lastEventId)
        }

        eventSource = factory.newEventSource(
            requestBuilder.build(),
            object : EventSourceListener() {
                override fun onEvent(
                    eventSource: EventSource,
                    id: String?,
                    type: String?,
                    data: String
                ) {
                    if (type == "resync_required") {
                        onResyncRequired()
                        return
                    }
                    onEvent(
                        MobileEvent(
                            cursor = id ?: "",
                            event = type ?: "message",
                            data = data,
                            timestamp = System.currentTimeMillis().toString()
                        )
                    )
                }

                override fun onFailure(
                    eventSource: EventSource,
                    t: Throwable?,
                    response: Response?
                ) {
                    if (response?.code == 401) {
                        tokenStorage.clearCredentials()
                        onTerminalUnauthorized()
                    }
                }
            }
        )
    }

    fun stopEventStream() {
        eventSource?.cancel()
        eventSource = null
    }
}

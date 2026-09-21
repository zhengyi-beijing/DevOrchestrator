package com.devorchestrator.mobile.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat
import com.devorchestrator.mobile.model.AlertPolicy
import com.devorchestrator.mobile.model.NotificationItem
import java.time.LocalTime
import java.time.ZoneOffset

class MobileNotificationService(private val context: Context) {
    private val notificationManager =
        context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    init {
        createChannels()
    }

    private fun createChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val progressChannel = NotificationChannel(
                CHANNEL_PROGRESS,
                "DevOrchestrator Progress",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Task completions, owner gates, and authoritative stalls"
            }

            val transportChannel = NotificationChannel(
                CHANNEL_TRANSPORT,
                "DevOrchestrator Connectivity",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Gateway disconnects and reconnection status"
            }

            notificationManager.createNotificationChannel(progressChannel)
            notificationManager.createNotificationChannel(transportChannel)
        }
    }

    fun isQuietHours(policy: AlertPolicy): Boolean {
        if (!policy.quiet_hours_enabled) return false
        try {
            val nowUtc = LocalTime.now(ZoneOffset.UTC)
            val start = LocalTime.parse(policy.quiet_hours_start_utc)
            val end = LocalTime.parse(policy.quiet_hours_end_utc)
            return if (start.isBefore(end)) {
                !nowUtc.isBefore(start) && nowUtc.isBefore(end)
            } else {
                !nowUtc.isBefore(start) || nowUtc.isBefore(end)
            }
        } catch (e: Exception) {
            return false
        }
    }

    fun showNotification(item: NotificationItem, policy: AlertPolicy) {
        val quiet = isQuietHours(policy)
        val channelId = if (item.family == "progress") CHANNEL_PROGRESS else CHANNEL_TRANSPORT

        val builder = NotificationCompat.Builder(context, channelId)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(item.title)
            .setContentText(item.message)
            .setPriority(
                if (quiet) NotificationCompat.PRIORITY_MIN
                else if (item.severity == "critical") NotificationCompat.PRIORITY_HIGH
                else NotificationCompat.PRIORITY_DEFAULT
            )
            .setSilent(quiet || !policy.sound_enabled)
            .setAutoCancel(true)

        // Deduplicate using deterministic hash of alert_key
        val notificationId = item.alert_key.hashCode()
        notificationManager.notify(notificationId, builder.build())
    }

    fun cancelNotification(alertKey: String) {
        notificationManager.cancel(alertKey.hashCode())
    }

    companion object {
        const val CHANNEL_PROGRESS = "devo_progress_channel"
        const val CHANNEL_TRANSPORT = "devo_transport_channel"
    }
}

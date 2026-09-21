package com.devorchestrator.mobile.model

data class PairRequest(
    val pairing_id: String,
    val code: String,
    val device_label: String
)

data class PairResponse(
    val device_id: String,
    val token: String,
    val scope: String,
    val expires_at: String?
)

data class ProjectSummary(
    val project_id: String,
    val name: String,
    val progress_observation_state: String,
    val status: String
)

data class RecoveryEpoch(
    val epoch_id: String,
    val timestamp: String
)

data class ProjectDetail(
    val project_id: String,
    val progress_observation_state: String,
    val watchdog_state: String?,
    val recovery_epoch: RecoveryEpoch?,
    val available_actions: List<String>,
    val task_status: String?,
    val plan_status: String?,
    val current_revision: Int
)

data class SubmitControlRequest(
    val action: String,
    val expected_revision: Int,
    val target_id: String? = null,
    val device_request_id: String
)

data class ControlCommandResponse(
    val command_id: String,
    val status: String,
    val result: String? = null,
    val error: String? = null
)

data class AlertPolicy(
    val quiet_hours_enabled: Boolean = false,
    val quiet_hours_start_utc: String = "22:00",
    val quiet_hours_end_utc: String = "08:00",
    val sound_enabled: Boolean = true,
    val stall_threshold_seconds: Int = 300
)

data class MobileEvent(
    val cursor: String,
    val event: String,
    val data: String,
    val timestamp: String
)

data class NotificationItem(
    val alert_key: String,
    val family: String, // "progress" or "transport"
    val title: String,
    val message: String,
    val severity: String // "info", "warning", "critical"
)

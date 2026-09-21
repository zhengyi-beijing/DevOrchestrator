package com.devorchestrator.mobile.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.devorchestrator.mobile.data.EncryptedTokenStorage
import com.devorchestrator.mobile.data.MobileApiClient
import com.devorchestrator.mobile.model.AlertPolicy
import com.devorchestrator.mobile.model.NotificationItem
import com.devorchestrator.mobile.model.ProjectDetail
import com.devorchestrator.mobile.model.ProjectSummary
import com.devorchestrator.mobile.service.MobileNotificationService
import org.json.JSONObject
import java.util.UUID

class MainActivity : ComponentActivity() {
    private lateinit var tokenStorage: EncryptedTokenStorage
    private lateinit var apiClient: MobileApiClient
    private lateinit var notificationService: MobileNotificationService

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        tokenStorage = EncryptedTokenStorage(applicationContext)
        apiClient = MobileApiClient(tokenStorage)
        notificationService = MobileNotificationService(applicationContext)

        setContent {
            MaterialTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    DevOrchestratorApp(tokenStorage, apiClient, notificationService)
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        apiClient.stopEventStream()
    }
}

@Composable
fun DevOrchestratorApp(
    tokenStorage: EncryptedTokenStorage,
    apiClient: MobileApiClient,
    notificationService: MobileNotificationService
) {
    var isAuthenticated by remember { mutableStateOf(tokenStorage.hasValidCredentials()) }
    var selectedProject by remember { mutableStateOf<String?>(null) }
    var alertPolicy by remember { mutableStateOf(AlertPolicy()) }

    LaunchedEffect(isAuthenticated) {
        if (isAuthenticated) {
            apiClient.startEventStream(
                onEvent = { event ->
                    try {
                        val json = JSONObject(event.data)
                        if (event.event == "alert") {
                            val alertKey = json.optString("alert_key", event.cursor)
                            val family = json.optString("family", "progress")
                            val title = json.optString("title", "DevOrchestrator Alert")
                            val message = json.optString("message", "DevOrchestrator alert")
                            val severity = json.optString("severity", "warn")
                            val item = NotificationItem(
                                alert_key = alertKey,
                                family = family,
                                title = title,
                                message = message,
                                severity = severity
                            )
                            notificationService.showNotification(item, alertPolicy)
                        } else if (event.event == "progress") {
                            val milestone = json.optString("milestone")
                            if (milestone == "OWNER_GATE" || milestone == "BLOCKED") {
                                val projId = json.optString("project_id", "project")
                                val item = NotificationItem(
                                    alert_key = "progress:milestone:$projId:$milestone",
                                    family = "progress",
                                    title = "Owner Action Required: $projId",
                                    message = json.optString("message", "Milestone: $milestone"),
                                    severity = if (milestone == "OWNER_GATE") "critical" else "warn"
                                )
                                notificationService.showNotification(item, alertPolicy)
                            }
                        }
                    } catch (e: Exception) {
                        // ignore malformed event payload
                    }
                },
                onResyncRequired = {},
                onTerminalUnauthorized = {
                    isAuthenticated = false
                    selectedProject = null
                }
            )
        } else {
            apiClient.stopEventStream()
        }
    }

    if (!isAuthenticated) {
        PairingScreen(
            onPaired = {
                isAuthenticated = true
            },
            apiClient = apiClient
        )
    } else if (selectedProject != null) {
        ProjectDetailScreen(
            projectId = selectedProject!!,
            apiClient = apiClient,
            onBack = { selectedProject = null },
            onUnauthorized = {
                isAuthenticated = false
                selectedProject = null
            }
        )
    } else {
        ProjectListScreen(
            apiClient = apiClient,
            onSelectProject = { selectedProject = it },
            onUnauthorized = { isAuthenticated = false }
        )
    }
}

@Composable
fun PairingScreen(onPaired: () -> Unit, apiClient: MobileApiClient) {
    var gatewayUrl by remember { mutableStateOf("http://100.64.0.1:8766") }
    var pairingId by remember { mutableStateOf("") }
    var pairingCode by remember { mutableStateOf("") }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var isLoading by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text("DevOrchestrator Pairing", style = MaterialTheme.typography.headlineMedium)
        Spacer(modifier = Modifier.height(16.dp))

        OutlinedTextField(
            value = gatewayUrl,
            onValueChange = { gatewayUrl = it },
            label = { Text("Gateway URL (Tailscale IP:Port)") },
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = pairingId,
            onValueChange = { pairingId = it },
            label = { Text("Pairing ID") },
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = pairingCode,
            onValueChange = { pairingCode = it },
            label = { Text("Pairing Code") },
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(modifier = Modifier.height(16.dp))

        if (errorMessage != null) {
            Text(errorMessage!!, color = MaterialTheme.colorScheme.error)
            Spacer(modifier = Modifier.height(8.dp))
        }

        Button(
            onClick = {
                isLoading = true
                errorMessage = null
                apiClient.pair(
                    gatewayUrl = gatewayUrl,
                    pairingId = pairingId.trim(),
                    code = pairingCode.trim(),
                    deviceLabel = "Android " + android.os.Build.MODEL
                ) { result ->
                    isLoading = false
                    result.onSuccess { onPaired() }
                    result.onFailure { errorMessage = it.message ?: "Pairing failed" }
                }
            },
            enabled = !isLoading && pairingId.isNotBlank() && pairingCode.isNotBlank(),
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(if (isLoading) "Pairing..." else "Pair Device")
        }
    }
}

@Composable
fun ProjectListScreen(
    apiClient: MobileApiClient,
    onSelectProject: (String) -> Unit,
    onUnauthorized: () -> Unit
) {
    var projects by remember { mutableStateOf<List<ProjectSummary>>(emptyList()) }
    var isLoading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        apiClient.getProjects { result ->
            isLoading = false
            result.onSuccess { projects = it }
            result.onFailure {
                if (it is SecurityException) onUnauthorized()
                else error = it.message
            }
        }
    }

    Scaffold(
        topBar = {
            Text("Projects", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(16.dp))
        }
    ) { padding ->
        Box(modifier = Modifier.padding(padding).fillMaxSize()) {
            if (isLoading) {
                CircularProgressIndicator(modifier = Modifier.align(Alignment.Center))
            } else if (error != null) {
                Text("Error: $error", color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(16.dp))
            } else {
                LazyColumn(modifier = Modifier.fillMaxSize()) {
                    items(projects) { p ->
                        Card(
                            onClick = { onSelectProject(p.project_id) },
                            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Column(modifier = Modifier.padding(16.dp)) {
                                Text(p.name, style = MaterialTheme.typography.titleMedium)
                                Spacer(modifier = Modifier.height(4.dp))
                                Row(horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
                                    Text("Status: ${p.status}", style = MaterialTheme.typography.bodySmall)
                                    Text(
                                        p.progress_observation_state,
                                        style = MaterialTheme.typography.labelSmall,
                                        color = if (p.progress_observation_state == "authoritative") Color(0xFF2E7D32) else Color.Gray
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun ProjectDetailScreen(
    projectId: String,
    apiClient: MobileApiClient,
    onBack: () -> Unit,
    onUnauthorized: () -> Unit
) {
    // Supported mobile control actions
    val supportedActions = listOf(
        "continue", "pause", "resume", "stop", "retry", "reconcile", "approve_owner_gate"
    )
    var currentRevision by remember { mutableIntStateOf(1) }
    var actionStatus by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        Button(onClick = onBack) { Text("Back") }
        Spacer(modifier = Modifier.height(16.dp))
        Text("Project: $projectId", style = MaterialTheme.typography.headlineSmall)
        Spacer(modifier = Modifier.height(8.dp))

        if (actionStatus != null) {
            Text("Action: $actionStatus", style = MaterialTheme.typography.bodyMedium)
            Spacer(modifier = Modifier.height(8.dp))
        }

        Text("Guarded Controls:", style = MaterialTheme.typography.titleMedium)
        Spacer(modifier = Modifier.height(8.dp))

        supportedActions.forEach { action ->
            Button(
                onClick = {
                    actionStatus = "Submitting $action..."
                    apiClient.submitControl(
                        projectId = projectId,
                        action = action,
                        expectedRevision = currentRevision,
                        deviceRequestId = UUID.randomUUID().toString()
                    ) { res ->
                        res.onSuccess {
                            actionStatus = "Submitted $action (cmd: ${it.command_id})"
                            currentRevision++
                        }
                        res.onFailure {
                            if (it is SecurityException) onUnauthorized()
                            else actionStatus = "Failed $action: ${it.message}"
                        }
                    }
                },
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 4.dp)
            ) {
                Text(action)
            }
        }
    }
}

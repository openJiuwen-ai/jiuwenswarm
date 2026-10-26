# Logging System

JiuwenSwarm provides a comprehensive logging system to record system operation status, debugging information, and audit logs, helping users monitor system operation, troubleshoot issues, and analyze system behavior.

## 1. Logging Basics

### 1.1 Storage Location

By default, JiuwenSwarm log files are stored in the following location:

```
~/.jiuwenswarm/agent/.logs/
```

### 1.2 Log File Classification

The logging system categorizes logs by component type and stores them in different files:

| Log File | Content |
|---------|---------|
| `gateway.log` | Gateway-related logs, including modules under `app`, `gateway`, `evolution`, `utils`, etc. |
| `channel.log` | Channel-related logs, including all modules under `channels` |
| `agent_server.log` | Agent server logs, including modules under `agents` and `.server` |
| `full.log` | Aggregation of all component logs |
| `desktop.log` | Desktop application logs |
| `permissions.log` | Permission-related logs |
| `ws-dev.log` | Web service development mode logs |

### 1.3 Log Content Types

The logging system has two main content types:

#### 1.3.1 General Logs

General logs are categorized by component and record system operation status, error messages, debugging information, etc.:
- Implemented using the standard Python logging module
- Stored in different files based on component types (see Section 1.2)

#### 1.3.2 Audit Logs

Audit logs record sandbox operation details in structured JSONL format, including:
- Command execution (`exec_command`)
- File transfer (`file_transfer`)
- Network requests (`network_request`)

Each audit log contains operation type, parameters, results, execution time, etc.

For sandbox operation audit logs, the storage location can be specified through configuration:
- Default: Not set (audit logs are not persisted)
- Can be specified via `--save-logs DIR` parameter or `JIUWENBOX_SAVE_LOGS_DIR` environment variable
- File name format: `{sandbox_id}-{YYYYMMDDTHHMMSS}.audit.log`

## 2. Viewing Logs

### 2.1 Frontend Log Viewing

![jiuwenswarm frontend logs](../assets/images/jiuwenswarm前端日志.png)

### 2.2 Real-time Log Viewing

Use the `tail` command to view logs in real-time:

```bash
# View full logs
tail -f ~/.jiuwenswarm/agent/.logs/full.log

# View specific component logs
tail -f ~/.jiuwenswarm/agent/.logs/gateway.log
```

### 2.3 Viewing Historical Logs

Use `cat` to view logs:

```bash
# View complete log file
cat ~/.jiuwenswarm/agent/.logs/full.log
```

### 2.4 Log Searching

Use the `grep` command to search log content:

```bash
# Search logs containing "error"
grep -i "error" ~/.jiuwenswarm/agent/.logs/full.log

# Search logs for a specific time range
grep "2026-05-19 15:" ~/.jiuwenswarm/agent/.logs/full.log
```

### 2.5 Viewing Audit Logs

```bash
# View audit logs
cat /var/log/jiuwenbox/9284a4bf-870-20260515T112345.audit.log

# View audit logs with jq formatting
jq '.' /var/log/jiuwenbox/9284a4bf-870-20260515T112345.audit.log
```

## 3. Log Rotation Strategy

The logging system adopts the following rotation strategy:

- **Size Limit**: Default maximum 20MB per log file (configurable via `max_bytes`)
- **Retention Count**: Default 20 log files retained (configurable via `backup_count`)
- **Automatic Rotation**: When a log file reaches the size limit, a new file is automatically created and old files are archived
- **Naming Format**: Archived files are named `{filename}.{index}`, e.g., `gateway.log.1`

## 4. Log System Architecture

### 4.1 Core Modules

- **Log Configuration**: `setup_logger` function in `jiuwenclaw/common/utils.py`
- **Audit Logs**: `jiuwenbox/src/jiuwenbox/server/audit_logger.py`
- **Default Log Implementation**: `openjiuwen/core/common/logging/default/default_impl.py`

### 4.2 Log Flow

1. Each module obtains a logger through `logging.getLogger(__name__)`
2. Automatically classified into different components based on logger name
3. Logs are output to both console and corresponding component log files
4. All logs are aggregated into `full.log`
5. Automatic rotation when log files reach size limit

## 5. Log Configuration

### 5.1 Configuration File

Log levels are mainly configured through the `logging` section in the `config.yaml` file:

```yaml
logging:
  level: INFO            # Default log level
  console_level: INFO    # Console log level
  gateway: INFO          # Gateway component log level
  channel: INFO          # Channel component log level
  agent_server: INFO     # Agent server log level
  full: INFO             # Full log level
  max_bytes: 20971520    # Log file size limit (20MB)
  backup_count: 20       # Number of log files to retain
```

### 5.2 Environment Variables

The console log level can be overridden through environment variables:

```bash
LOG_LEVEL=DEBUG jiuwenswarm-start
```

### 5.3 Command Line Parameters

When starting the service, log level can be specified via parameters:

```bash
jiuwenswarm-start --log-level DEBUG
```

## 6. Log Levels

JiuwenSwarm supports standard Python logging levels:

| Level | Description |
|------|------|
| DEBUG | Debug information, used for development and debugging |
| INFO | General information, recording normal system operation status |
| WARNING | Warning information, recording potential issues |
| ERROR | Error information, recording system errors |
| CRITICAL | Critical error information, recording system crashes and other serious issues |

## 7. Log Format

The log format includes timestamp, log level, module name, and log message:

```
2026-05-19 15:30:45.123 INFO jiuwenclaw.app: Service started successfully
```

### 7.1 Audit Log Format

Audit logs use structured JSON format:

```json
{
  "timestamp": "2026-05-19T15:30:45.123Z",
  "event_type": "exec_command",
  "sandbox_id": "9284a4bf-870",
  "command": "ls -la",
  "workdir": "/home/user",
  "ok": true,
  "exit_code": 0,
  "stdout": "total 40\ndrwxr-xr-x  5 user user 4096 May 19 15:30 .",
  "stderr": "",
  "duration_ms": 123
}
```

## 8. Common Issues and Troubleshooting

### 8.1 Large Log Files

**Problem**: Log files grow too quickly and occupy too much disk space

**Solution**:
- Lower log level to reduce log output
- Decrease `max_bytes` configuration to reduce individual log file size
- Decrease `backup_count` configuration to reduce the number of retained log files

### 8.2 No Log Output

**Problem**: Cannot find log files or log content is empty

**Solution**:
- Check if log directory permissions are correct
- Check if log level configuration is too high
- Check if the service is started normally

### 8.3 Log Garbled Characters

**Problem**: Log files contain garbled characters

**Solution**:
- Ensure system encoding is set correctly (UTF-8 recommended)
- Use text editors that support UTF-8 encoding to view logs

## 9. Best Practices

1. **Development Environment**: Use DEBUG level to obtain detailed debugging information
2. **Production Environment**: Use INFO or WARNING level to reduce log output
3. **Regular Cleaning**: Regularly clean expired log files to avoid occupying too much disk space
4. **Centralized Management**: Consider using log collection tools (such as ELK Stack) for centralized log management
5. **Sensitive Information**: Be careful with sensitive information that may be included in logs, such as API keys, passwords, etc.

## 10. Related Configuration Files

- Main configuration file: `~/.jiuwenswarm/config/config.yaml`
- Environment variable file: `~/.jiuwenswarm/config/.env`
- Log system implementation: `jiuwenclaw/common/utils.py`
- Audit log implementation: `jiuwenbox/src/jiuwenbox/server/audit_logger.py`
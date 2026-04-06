# Troubleshooting Guide

Common issues and how to resolve them.

## Ollama Not Running / Not Installed

**Symptoms:** Provider health check fails. Error messages like "Ollama is not installed" or "Ollama is not responding."

**Fix (not installed):**
```bash
brew install ollama
ollama serve
ollama pull llama3.2
```

**Fix (installed but not running):**
```bash
# Start as a background service (preferred):
brew services start ollama

# Or start manually:
ollama serve
```

**Fix (running but model not pulled):**
```bash
ollama pull llama3.2
# Or whatever model is in your config.yaml under provider.ollama.model
```

**Verify it works:**
```bash
curl http://localhost:11434/api/tags
```

The dashboard and `secure-agents setup` will attempt to start Ollama automatically if it is installed but not running.

## Email Authentication Failures

### app_password method

**Symptoms:** "Authentication failed" or "Invalid credentials" when testing email connection.

**Common causes:**
- Using your regular Gmail password instead of an App Password.
- Gmail requires a 16-character App Password when 2FA is enabled.
- The credential is not stored in Keychain or environment.

**Fix:**
1. Go to https://myaccount.google.com/apppasswords (requires 2FA enabled).
2. Generate a new App Password for "Mail".
3. Store it:
   ```bash
   secure-agents auth setup
   # Enter the 16-character app password when prompted
   ```
4. Or set the environment variable: `export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"`

### oauth2 method

**Symptoms:** "No OAuth2 token" or "OAuth2 authentication failed."

**Fix:**
1. Ensure you have a `client_secrets.json` from Google Cloud Console (OAuth 2.0 Client ID, Desktop app type).
2. Run the OAuth flow:
   ```bash
   secure-agents auth gmail path/to/client_secrets.json
   ```
3. A browser window opens for authorization. Complete the Google sign-in.
4. Token is stored in `~/.secure-agents/tokens/` with restricted permissions.

**Token expired:**
Tokens auto-refresh. If refresh fails, re-run the `auth gmail` command.

### Wrong auth_method in config

Make sure `config.yaml` matches what you set up:
```yaml
defaults:
  email:
    imap:
      auth_method: app_password   # or "oauth2"
```

## Docker / Sandbox Issues

Sandbox is enabled by default and requires Docker. There is no subprocess fallback -- if Docker is missing and sandbox is enabled, the framework fails with a hard error.

### Docker Not Installed or Not Running

**Symptoms:** Hard error on agent start: "Docker is required for sandbox execution" or similar.

**Fix (not installed):**
```bash
# Install Docker Desktop: https://www.docker.com/products/docker-desktop/
# After installing, start Docker Desktop and ensure it is running.
```

**Fix (installed but not running):**
```bash
# Start Docker Desktop, or:
open -a Docker
# Wait for Docker to fully start, then verify:
docker info
```

**Verify Docker is available:**
```bash
docker info
docker ps
```

### Document Parsing Fails in Sandbox

**Symptoms:** `document_parser` returns errors about sandbox execution or container failures.

**Fix:**
1. Ensure Docker is running (`docker info`).
2. Check that the Docker image used by the sandbox is available:
   ```bash
   docker images
   ```
3. Check Docker disk space -- containers may fail if the disk is full.
4. Review logs for sandbox-specific errors:
   ```bash
   docker logs <container_id>
   ```

### Disabling Sandbox (Not Recommended)

If you must disable sandbox for debugging, set `sandbox_enabled: false` in `config.yaml`. This is not recommended for production use. Document parsing will run outside the sandbox, reducing security isolation.

## Dashboard Won't Start / Port in Use

**Symptoms:** "Address already in use" error when running `secure-agents ui`.

**Fix (use a different port):**
```bash
secure-agents ui --port 8421
```

**Fix (find and kill the process using the port):**
```bash
lsof -i :8420
kill <PID>
```

**Dashboard not opening in browser:**
```bash
# Disable auto-open and navigate manually:
secure-agents ui --no-browser
# Then open http://127.0.0.1:8420 in your browser
```

**Note:** The dashboard binds to 127.0.0.1 only (not 0.0.0.0) and has CORS restrictions with per-session auth tokens. It is not accessible from other machines on the network.

## Agent Stuck in Running State

**Symptoms:** An agent appears as "running" in the dashboard but is not processing work. Or an agent won't stop when requested.

**Possible causes:**
- The agent's `tick()` is blocking (using `time.sleep()` instead of `_stop_event.wait()`).
- A tool call is hanging (e.g., IMAP connection timeout).
- An unhandled exception in `tick()` is being caught by the base class but the agent keeps retrying.

**Fix:**
1. Check logs for `agent.tick_error` entries.
2. Stop the agent via the dashboard or CLI (Ctrl+C).
3. If the agent does not stop within 5 seconds, the process may need to be killed.
4. Check the metrics endpoint (`GET /api/metrics`) for error counts and tick latency.

**Prevention:**
- Always use `self._stop_event.wait(seconds)` instead of `time.sleep()`.
- Set reasonable timeouts on all network calls in tools.
- Log errors with metadata, not content.

## Config File Errors

**Symptoms:** "Config parse error" from `secure-agents validate`, or agents fail to start.

**Common issues:**

1. **Missing config.yaml:**
   ```bash
   cp config.example.yaml config.yaml
   ```

2. **YAML syntax error:** Check indentation. Use spaces, not tabs. Validate with:
   ```bash
   python -c "import yaml; yaml.safe_load(open('config.yaml'))"
   ```

3. **Agent not in config:** Every agent you want to start must have an entry under `agents:` in config.yaml, even if it just says `enabled: true`.

4. **Agent registered but not configured:**
   ```
   Agent 'my_agent' is configured but has no registered implementation.
   ```
   This means the config has the agent name but no Python class is registered. Check that:
   - The agent file exists in `src/secure_agents/agents/your_agent/agent.py`
   - The `@register_agent("your_agent")` decorator is applied
   - The agent module can be imported without errors

5. **Environment variable not set:** If config uses `${VAR}` and the variable is not set, the literal string `${VAR}` is kept. Use `${VAR:default}` to provide a fallback.

## Reading Audit Logs

Audit logs are stored at the path configured in `security.audit_log_path` (default: `./logs/audit.log`). Each line is a JSON object:

```bash
# View recent entries:
tail -20 logs/audit.log

# Pretty-print:
tail -20 logs/audit.log | python -m json.tool

# Filter by event type:
grep '"event": "nda_detected"' logs/audit.log

# Filter by time range (entries have a "timestamp" field as Unix epoch):
cat logs/audit.log | python -c "
import json, sys, time
cutoff = time.time() - 3600  # last hour
for line in sys.stdin:
    entry = json.loads(line)
    if entry['timestamp'] > cutoff:
        print(json.dumps(entry, indent=2))
"
```

Audit logs record metadata only (filenames, senders, event types, risk scores) -- never document content or PII.

## Checking Metrics

Metrics are available via the dashboard API while the server is running:

```bash
curl http://127.0.0.1:8420/api/metrics | python -m json.tool
```

The response includes per-agent stats: tick count, error count, error rate, uptime, and latency percentiles (p50, p95, p99).

Metrics are held in memory and reset on server restart.

## Using secure-agents validate

The `validate` command checks everything at once:

```bash
secure-agents validate
```

It checks:
- Config file exists and parses correctly
- Ollama is reachable (the only supported provider -- no cloud providers)
- All registered agents, tools, and providers are listed
- Configured agents are shown with enabled/disabled status
- Docker availability (required -- sandbox is enabled by default)
- Agent name validity (lowercase alphanumeric + underscores only)

Use this as a first step when anything is not working.

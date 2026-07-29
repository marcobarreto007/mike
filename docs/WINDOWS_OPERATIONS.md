# Mike Windows operations

All operational scripts resolve the repository from their own location. The
workspace can be moved without editing a drive-specific path.

## Runtime endpoints

- Mike dashboard/API: `http://127.0.0.1:8083`
- Qwen 3.6 OpenAI-compatible backend: `http://127.0.0.1:8081`
- Mike health: `http://127.0.0.1:8083/health`
- Qwen health: `http://127.0.0.1:8081/health`

## Normal process-mode operation

Start both Qwen and Mike, without requiring the public tunnel:

```powershell
.\scripts\ops\launch_mike.ps1 -SkipTunnel
```

Inspect status without changing the runtime:

```powershell
.\scripts\ops\recover_mike.ps1 -Mode status
```

Restart both components:

```powershell
.\scripts\ops\recover_mike.ps1 -Mode restart -SkipTunnel
```

Recovery scripts only stop a Python process when its command line points to
`core\server\mike_server.py` in this exact workspace. If another application
owns port 8083, recovery fails with its executable and command line instead of
terminating it.

## Optional Windows services

The service installer defines two automatic NSSM services:

1. `MikeQwen36` runs the Qwen 3.6 llama server on port 8081.
2. `MikeServer` runs Mike on port 8083 and declares `MikeQwen36` as a service
   dependency.

NSSM must be installed and available in `PATH`. Preview and validate the
configuration without requesting elevation or changing services:

```powershell
.\scripts\ops\install_mike_service.ps1 -WhatIf
```

Install the services but leave them stopped:

```powershell
.\scripts\ops\install_mike_service.ps1
```

Existing NSSM services with these names are updated in place. If either name
belongs to a service not managed by NSSM, installation stops with a collision
diagnostic and does not replace that service.

Use `-StartNow` only after the process-mode runtimes on ports 8081 and 8083
have been explicitly stopped.

The Cloudflare tunnel remains a separate optional service
(`CloudflaredMikeTunnel`) and is not required for local operation.

## Heartbeat tasks

`install_heartbeat.ps1` registers the heartbeat and morning briefing tasks.
Both invoke `mike_heartbeat.ps1`, which resolves the current workspace,
configures Mike's Python import paths, and runs
`core\autonomy\mike_heartbeat.py`.

Validate the heartbeat environment without running its external checks:

```powershell
.\scripts\ops\mike_heartbeat.ps1 -ValidateOnly
```

## Non-destructive validation

```powershell
.\scripts\ops\test_ops_hardening.ps1
```

This parses every PowerShell operational script, rejects legacy workspace,
service-name, and port defaults, verifies the two-service dependency, and
checks process identity without stopping anything.

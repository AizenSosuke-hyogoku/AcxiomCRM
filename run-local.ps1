$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
  py -3.12 -m venv .venv
}
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt -r ai_service\requirements.txt
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
$EscRoot = $ProjectRoot.Replace("'", "''")
$EscPython = $Python.Replace("'", "''")
$AiCommand = "Set-Location '$EscRoot\ai_service'; & '$EscPython' -m uvicorn main:app --host 127.0.0.1 --port 8001"
$WebCommand = "Set-Location '$EscRoot'; & '$EscPython' -m uvicorn app.main:app --host 127.0.0.1 --port 8080"
Start-Process powershell.exe -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-Command',$AiCommand)
Start-Sleep -Seconds 2
Start-Process powershell.exe -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-Command',$WebCommand)
Write-Host ''
Write-Host 'AcxiomCRM is starting. Open http://localhost:8080' -ForegroundColor Cyan
Write-Host 'Keep both service windows open. Close them to stop the demo.'

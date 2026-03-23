# Statusify launcher builder
# Run once: right-click -> Run with PowerShell

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$icoPath   = Join-Path $scriptDir "statusify.ico"
$exePath   = Join-Path $scriptDir "Statusify.exe"

# Find pythonw.exe via registry
$pythonw = ""
foreach ($v in @("3.13","3.12","3.11","3.10","3.9","3.8")) {
    foreach ($hive in @("HKCU","HKLM")) {
        $reg = "${hive}:\SOFTWARE\Python\PythonCore\$v\InstallPath"
        if (Test-Path $reg) {
            try {
                $py = (Get-ItemProperty $reg -ErrorAction Stop)."(default)"
                $candidate = Join-Path $py "pythonw.exe"
                if (Test-Path $candidate) { $pythonw = $candidate; break }
            } catch {}
        }
    }
    if ($pythonw) { break }
}
if (-not $pythonw) {
    try { $pythonw = (Get-Command pythonw.exe).Source } catch {}
}
if (-not $pythonw) {
    Write-Host "ERROR: pythonw.exe not found. Is Python installed?" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Found pythonw: $pythonw"

$pyEscaped = $pythonw -replace '\\','\\\\'

$cs = @"
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
[assembly: AssemblyTitle("Statusify")]
[assembly: AssemblyProduct("Statusify")]
[assembly: AssemblyDescription("Statusify")]
[assembly: AssemblyFileVersion("1.0.0.0")]
[assembly: AssemblyInformationalVersion("1.0.0")]
class Program {
    [STAThread]
    static void Main() {
        string dir = Path.GetDirectoryName(
            System.Reflection.Assembly.GetExecutingAssembly().Location);
        string script = Path.Combine(dir, "main.py");
        var p = new ProcessStartInfo();
        p.FileName = @"$pyEscaped";
        p.Arguments = "\"" + script + "\"";
        p.WorkingDirectory = dir;
        p.WindowStyle = ProcessWindowStyle.Hidden;
        p.CreateNoWindow = true;
        Process.Start(p);
    }
}
"@

$provider = New-Object Microsoft.CSharp.CSharpCodeProvider
$params   = New-Object System.CodeDom.Compiler.CompilerParameters
$params.OutputAssembly     = $exePath
$params.GenerateExecutable = $true
$params.CompilerOptions    = "/target:winexe"
if (Test-Path $icoPath) {
    $params.CompilerOptions += " /win32icon:`"$icoPath`""
    Write-Host "Using icon: $icoPath"
}
$params.ReferencedAssemblies.Add("System.dll") | Out-Null

Write-Host "Compiling Statusify.exe..."
$result = $provider.CompileAssemblyFromSource($params, $cs)

if ($result.Errors.HasErrors) {
    Write-Host "FAILED:" -ForegroundColor Red
    foreach ($err in $result.Errors) { Write-Host $err.ToString() -ForegroundColor Red }
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "SUCCESS: $exePath" -ForegroundColor Green
Write-Host "Now toggle startup off and on in Statusify Settings." -ForegroundColor Yellow
Read-Host "Press Enter to exit"

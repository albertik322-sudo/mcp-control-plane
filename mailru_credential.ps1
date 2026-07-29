[CmdletBinding(DefaultParameterSetName = "Set")]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9_-]{0,31}$')]
    [string]$AccountId,

    [Parameter(Mandatory = $true, ParameterSetName = "Set")]
    [ValidatePattern('^[^\s@]+@[^\s@]+$')]
    [string]$Address,

    [Parameter(ParameterSetName = "Set")]
    [ValidateSet("mailru", "yandex")]
    [string]$Provider = "mailru",

    [Parameter(ParameterSetName = "Set")]
    [ValidateLength(1, 320)]
    [string]$Login,

    [Parameter(Mandatory = $true, ParameterSetName = "Remove")]
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$prefix = if ($Provider -eq "yandex") { "CodexYandex" } else { "CodexMailru" }
$target = "$prefix/$AccountId"

if (-not ('MailruCredential.NativeMethods' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace MailruCredential {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL {
        public UInt32 Flags;
        public UInt32 Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public UInt32 CredentialBlobSize;
        public IntPtr CredentialBlob;
        public UInt32 Persist;
        public UInt32 AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    public static class NativeMethods {
        [DllImport("Advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool CredWrite(ref CREDENTIAL credential, UInt32 flags);

        [DllImport("Advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool CredDelete(string target, UInt32 type, UInt32 flags);
    }
}
'@
}

if ($Remove) {
    $deleted = [MailruCredential.NativeMethods]::CredDelete($target, 1, 0)
    if (-not $deleted) {
        $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($code -ne 1168) {
            throw "Не удалось удалить запись Credential Manager (код $code)."
        }
    }
    Write-Host "Запись $target удалена."
    exit 0
}

$securePassword = Read-Host "Введите отдельный пароль приложения Mail.ru" -AsSecureString
$passwordPtr = [Runtime.InteropServices.Marshal]::SecureStringToCoTaskMemUnicode($securePassword)
try {
    $password = [Runtime.InteropServices.Marshal]::PtrToStringUni($passwordPtr)
    if ([string]::IsNullOrWhiteSpace($password)) {
        throw "Пароль приложения не может быть пустым."
    }
    $blob = [Text.Encoding]::Unicode.GetBytes($password)
    if ($blob.Length -gt 512) {
        throw "Пароль приложения превышает лимит Windows Credential Manager."
    }
    $blobPtr = [Runtime.InteropServices.Marshal]::AllocCoTaskMem($blob.Length)
    try {
        [Runtime.InteropServices.Marshal]::Copy($blob, 0, $blobPtr, $blob.Length)
        $credential = New-Object MailruCredential.CREDENTIAL
        $credential.Type = 1
        $credential.TargetName = $target
        $credential.UserName = if ($Login) { $Login } else { $Address }
        $credential.CredentialBlobSize = $blob.Length
        $credential.CredentialBlob = $blobPtr
        $credential.Persist = 2
        if (-not [MailruCredential.NativeMethods]::CredWrite([ref]$credential, 0)) {
            $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw "Не удалось сохранить запись Credential Manager (код $code)."
        }
    }
    finally {
        if ($blobPtr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::Copy((New-Object byte[] $blob.Length), 0, $blobPtr, $blob.Length)
            [Runtime.InteropServices.Marshal]::FreeCoTaskMem($blobPtr)
        }
        if ($null -ne $blob) {
            [Array]::Clear($blob, 0, $blob.Length)
        }
    }
}
finally {
    if ($passwordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeCoTaskMemUnicode($passwordPtr)
    }
    $password = $null
    $securePassword = $null
}

Write-Host "Пароль приложения сохранён локально как $target; само значение не выводилось."

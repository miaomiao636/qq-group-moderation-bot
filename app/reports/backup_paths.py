"""Private backup roots and non-following path checks; no production ACL changes."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from app.core.fs_guard import is_link_like


def checked(path: Path, *, exists: bool = True) -> Path:
    if ".." in path.parts:
        raise ValueError("PARENT_PATH_REFUSED")
    path = path.absolute()
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            if is_link_like(item):
                raise ValueError("LINK_PATH_REFUSED")
        elif item == path and exists:
            raise ValueError("SOURCE_MISSING")
    return path


def private_directory(path: Path, owner_sid: str | None = None) -> Path:
    path = checked(path, exists=False)
    created = not path.exists()
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not path.is_dir():
        raise ValueError("PRIVATE_DIRECTORY_REQUIRED")
    verify_private(path, owner_sid, create=created, directory=True)
    return path


def verify_private(
    path: Path, owner_sid: str | None = None, *, create: bool = False, directory: bool = False
) -> None:
    checked(path)
    if os.name == "nt":
        # An explicit env argument transports paths without shell interpolation.
        script = r"""
$ErrorActionPreference='Stop'
$p=$env:QQBOT_BACKUP_PRIVATE_PATH
$sid=$env:QQBOT_BACKUP_OWNER_SID
if(-not $sid) { $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
$allowed=@('S-1-5-18','S-1-5-32-544',$sid) | Select-Object -Unique
if($env:QQBOT_BACKUP_PRIVATE_NEW -eq '1') {
 $acl=[System.IO.Directory]::GetAccessControl($p)
 $acl.SetAccessRuleProtection($true,$false)
 foreach($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
 foreach($identity in $allowed) {
  $rule=New-Object System.Security.AccessControl.FileSystemAccessRule(
   [System.Security.Principal.SecurityIdentifier]$identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
  $acl.AddAccessRule($rule)
 }
 [System.IO.Directory]::SetAccessControl($p,$acl)
}
if([System.IO.Directory]::Exists($p)) { $acl=[System.IO.Directory]::GetAccessControl($p) }
else { $acl=[System.IO.File]::GetAccessControl($p) }
$owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
if($owner -notin $allowed) { throw 'UNTRUSTED_OWNER' }
if($env:QQBOT_BACKUP_PRIVATE_DIRECTORY -eq '1' -and -not $acl.AreAccessRulesProtected) { throw 'INHERITED_ROOT' }
$rules=$acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])
if($rules.Count -eq 0) { throw 'EMPTY_ACL' }
foreach($r in $rules) {
 if($r.AccessControlType -ne 'Allow' -or $r.IdentityReference.Value -notin $allowed) { throw 'NONPRIVATE_ACL' }
}
foreach($identity in $allowed) {
 $rights=0
 foreach($r in $rules) {
  if($r.IdentityReference.Value -eq $identity -and ($r.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0) { $rights=$rights -bor [int]$r.FileSystemRights }
 }
 if(($rights -band 2032127) -ne 2032127) { throw 'MISSING_FULL_CONTROL' }
}
"""
        env = dict(
            os.environ,
            QQBOT_BACKUP_PRIVATE_PATH=str(path),
            QQBOT_BACKUP_PRIVATE_NEW="1" if create else "0",
            QQBOT_BACKUP_PRIVATE_DIRECTORY="1" if directory else "0",
            QQBOT_BACKUP_OWNER_SID=owner_sid or "",
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise PermissionError("PRIVATE_ACL_REQUIRED")
    elif path.stat().st_mode & 0o077:
        raise PermissionError("PRIVATE_MODE_REQUIRED")


def regular(path: Path) -> Path:
    path = checked(path)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("REGULAR_FILE_REQUIRED")
    return path

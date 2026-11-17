using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Principal;

namespace MemULife.Capture;

internal static class SpoolPrivacy
{
    public static void CreateRoot(string path)
    {
        if (!OperatingSystem.IsWindows()) { Directory.CreateDirectory(path); return; }
        CreateWindowsRoot(path);
    }

    [SupportedOSPlatform("windows")]
    private static void CreateWindowsRoot(string path)
    {
        if (!Directory.Exists(path))
        {
            using var identity = WindowsIdentity.GetCurrent();
            var user = identity.User ?? throw new CaptureFailure("spool_permissions_not_private");
            var security = new DirectorySecurity();
            security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
            security.SetOwner(user);
            security.AddAccessRule(new FileSystemAccessRule(user, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            // Directory creation receives the DACL atomically, before any media exists.
            security.CreateDirectory(path);
        }
        VerifyWindows(path, requireProtected: true);
    }

    public static void Verify(string path)
    {
        if (OperatingSystem.IsWindows() && (Directory.Exists(path) || File.Exists(path)))
            VerifyWindows(path, requireProtected: false);
    }

    [SupportedOSPlatform("windows")]
    private static void VerifyWindows(string path, bool requireProtected)
    {
        using var identity = WindowsIdentity.GetCurrent();
        var user = identity.User ?? throw new CaptureFailure("spool_permissions_not_private");
        FileSystemSecurity security = Directory.Exists(path)
            ? new DirectoryInfo(path).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner)
            : new FileInfo(path).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
        if (requireProtected && (!user.Equals(security.GetOwner(typeof(SecurityIdentifier)))
            || !security.AreAccessRulesProtected))
            throw new CaptureFailure("spool_permissions_not_private");
        bool currentUserAllowed = false;
        foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
        {
            if (rule.AccessControlType != AccessControlType.Allow) continue;
            if (!user.Equals(rule.IdentityReference)) throw new CaptureFailure("spool_permissions_not_private");
            currentUserAllowed |= (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl;
        }
        // Empty/null ACLs cannot pass as private simply because there were no ACEs.
        if (!currentUserAllowed) throw new CaptureFailure("spool_permissions_not_private");
    }
}

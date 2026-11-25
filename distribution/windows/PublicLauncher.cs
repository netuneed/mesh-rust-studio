using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Mesh Rust Studio")]
[assembly: AssemblyProduct("Mesh Rust Studio")]

internal static class PublicLauncher
{
    private const string Name = "Mesh Rust Studio";
    private const string ProgId = "MeshRustStudio.Project";
    private const string Capabilities = @"Software\MeshRustStudio\Capabilities";
    private const string WorkRegistry = @"Software\Classes\MeshRustStudio.Project";

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string folder = AppDomain.CurrentDomain.BaseDirectory;
            string edition = File.ReadAllText(Path.Combine(folder, "edition.txt")).Trim();
            if (edition != "work" && edition != "dev")
                throw new InvalidDataException("The edition must be work or dev.");
            string executable = Path.Combine(folder, "blender.exe");
            if (!File.Exists(executable))
                throw new FileNotFoundException("Extract the entire archive before launching.", executable);

            if (args.Length == 1 && args[0] == "--default-apps" && edition == "work")
            {
                OfferFileAssociations();
                return 0;
            }
            return Launch(executable, edition, args);
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string ProjectPath(string[] args)
    {
        if (args.Length == 0) return null;
        if (args.Length != 1 || !Path.IsPathRooted(args[0]) || !File.Exists(args[0]))
            throw new ArgumentException("Select an existing .blend or .blend1 file.");
        string project = Path.GetFullPath(args[0]);
        string extension = Path.GetExtension(project);
        if (!String.Equals(extension, ".blend", StringComparison.OrdinalIgnoreCase) &&
            !String.Equals(extension, ".blend1", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Select an existing .blend or .blend1 file.");
        return project;
    }

    private static int Launch(string executable, string edition, string[] args)
    {
        string project = ProjectPath(args);
        string root = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        string roaming = Environment.GetEnvironmentVariable("APPDATA");
        if (String.IsNullOrEmpty(root) || !Path.IsPathRooted(root))
            throw new DirectoryNotFoundException("Local application data is unavailable.");
        string state = Path.Combine(root, "MeshRustStudio", edition);
        string profile = Path.Combine(state, "profile");
        Directory.CreateDirectory(Path.Combine(profile, "config"));
        string cache = Directory.CreateDirectory(Path.Combine(state, "cache")).FullName;
        string temp = Directory.CreateDirectory(Path.Combine(state, "temp")).FullName;
        var start = new ProcessStartInfo
        {
            FileName = executable,
            WorkingDirectory = Path.GetDirectoryName(executable),
            Arguments = "--disable-autoexec",
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        if (project != null)
            start.Arguments += " \"" + project + "\"";

        var env = start.EnvironmentVariables;
        foreach (string key in env.Keys.Cast<string>()
            .Where(key => key.StartsWith("BLENDER_", StringComparison.OrdinalIgnoreCase) ||
                          key.StartsWith("PYTHON", StringComparison.OrdinalIgnoreCase)).ToArray())
            env.Remove(key);
        env["BLENDER_USER_RESOURCES"] = profile;
        env["BLENDER_RUST_PORT_CACHE_DIR"] = cache;
        env["BLENDER_RUST_PORT_TEMP_DIR"] = temp;
        env["BLENDER_RUST_PUBLIC"] = "1";
        env["BLENDER_RUST_PUBLIC_EDITION"] = edition;
        env["BLENDER_RUST_PORT_INSTANCE"] = edition;
        env["BLENDER_CUSTOM_SPLASH"] = Path.Combine(Path.GetDirectoryName(executable), "MeshRustStudio-splash.png");
        if (!String.IsNullOrEmpty(roaming) && Path.IsPathRooted(roaming))
            env["BLENDER_RUST_ORIGINAL_APPDATA"] = roaming;
        if (project != null) env["BLENDER_RUST_OPENING_PROJECT"] = "1";
        env["TEMP"] = temp;
        env["TMP"] = temp;
        using (var started = Process.Start(start))
            if (started == null) throw new InvalidOperationException("Could not start Mesh Rust Studio.");
        return 0;
    }

    private static void SetRegistry(string key, string name, string value)
    {
        using (var entry = Registry.CurrentUser.CreateSubKey(key))
            entry.SetValue(name, value, RegistryValueKind.String);
    }

    [DllImport("shell32.dll")]
    private static extern void SHChangeNotify(int eventId, uint flags, IntPtr first, IntPtr second);

    private static void OfferFileAssociations()
    {
        string launcher = Assembly.GetExecutingAssembly().Location;
        SetRegistry(WorkRegistry, "", "Blender project");
        SetRegistry(WorkRegistry + @"\DefaultIcon", "", "\"" + launcher + "\",0");
        SetRegistry(WorkRegistry + @"\shell\open\command", "", "\"" + launcher + "\" \"%1\"");
        SetRegistry(Capabilities, "ApplicationName", Name);
        SetRegistry(Capabilities, "ApplicationDescription", "Opens Blender project files in Mesh Rust Studio Work.");
        foreach (string extension in new[] { ".blend", ".blend1" })
        {
            SetRegistry(@"Software\Classes\" + extension + @"\OpenWithProgids", ProgId, "");
            SetRegistry(Capabilities + @"\FileAssociations", extension, ProgId);
        }
        SetRegistry(@"Software\RegisteredApplications", Name, Capabilities);
        const int associationChanged = 0x08000000;
        SHChangeNotify(associationChanged, 0, IntPtr.Zero, IntPtr.Zero);
        using (Process.Start(new ProcessStartInfo(
            "ms-settings:defaultapps?registeredAppUser=" + Uri.EscapeDataString(Name)) { UseShellExecute = true })) { }
    }
}

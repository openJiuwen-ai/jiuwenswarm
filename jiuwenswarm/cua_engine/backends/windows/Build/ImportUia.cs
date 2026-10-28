// Build-time only. Derive COM declarations from the host Windows type library;
// never maintain hand-written vtable layouts or ship a downloaded interop DLL.
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

sealed class ImportUia : ITypeLibImporterNotifySink {
    [DllImport("oleaut32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
    static extern void LoadTypeLibEx(string file, int kind, [MarshalAs(UnmanagedType.Interface)] out object library);
    public void ReportEvent(ImporterEventKind kind, int code, string message) {
        // NativeArray overloads contain pointer signatures that the automatic
        // importer cannot fully marshal. The driver uses only safe-array
        // overloads. Preserve warnings for diagnosis without flooding setup.
        if (kind != ImporterEventKind.NOTIF_TYPECONVERTED)
            File.AppendAllText("uia-import.log", kind + " " + code + ": " + message + Environment.NewLine);
    }
    public Assembly ResolveRef(object library) { throw new NotSupportedException("Unexpected external UIA type library dependency"); }
    [MTAThread] static void Main(string[] args) {
        Directory.SetCurrentDirectory(args[0]);
        File.WriteAllText("uia-import.log", "");
        object library;
        LoadTypeLibEx(Path.Combine(Environment.SystemDirectory, "UIAutomationCore.dll"), 2, out library);
        var assembly = new TypeLibConverter().ConvertTypeLibToAssembly(library, "Interop.NativeUIA.dll",
            TypeLibImporterFlags.SafeArrayAsSystemArray, new ImportUia(), null, null, "NativeUIA", null);
        assembly.Save("Interop.NativeUIA.dll");
    }
}

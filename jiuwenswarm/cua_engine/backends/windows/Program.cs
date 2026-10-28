using System;
using System.Linq;
using System.Text;

namespace ComputerUse.Windows {
    // Platform composition is separate from the protocol/session dispatcher.
    internal static class Program {
        static NativeReadStore workerStore;
        static NativeReadBackend workerReader;
        static object[] checkpoint = new object[0];
        static NativeRuntime CreateRuntime(string session, string mode) {
            var reader = new NativeReadBackend(session);
            workerReader = reader;
            reader.Store.RestoreWindows(checkpoint); workerStore=reader.Store;
            bool foregroundAllowed = Environment.GetEnvironmentVariable("COMPUTER_USE_FOREGROUND_ASSIST") == "1";
            var keyboard = new NativeKeyboard(new NativeUiaKeyboard(reader.Store), mode,
                foregroundAllowed, new NativeWindowsClipboard());
            var captures = new NativePointerCaptures();
            var pointer = new NativePointer(new NativeWindowsPointer(reader.Store, captures), mode, foregroundAllowed);
            var desktopKeyboard = mode == "exclusive" ? new NativeKeyboard(new NativeDesktopKeyboard(captures), mode, false) : null;
            return new NativeRuntime(session, reader, new NativeExecution(new NativeUiaObjects(reader.Store), keyboard, pointer,
                    new NativeScroll(new NativeUiaScroll(reader.Store)), desktopKeyboard),
                new NativeCapture(reader.Store, session, mode, captures));
        }
        [MTAThread] static int Main(string[] args) {
            Console.InputEncoding = new UTF8Encoding(false); Console.OutputEncoding = new UTF8Encoding(false); Native.Initialize();
            if (args.Contains("--check-permissions") || args.Contains("--request-permissions")) {
                bool available = Native.Interactive(); Console.WriteLine(Wire.Json(Wire.Obj("accessibility", available, "screenCapture", available, "input", available,
                    "platform", "windows", "detail", "interactive desktop availability only; individual providers and elevated targets are checked at operation time"))); return 0;
            }
            try {
                if (args.Contains("--native-worker")) {
                    string line=Console.ReadLine(); if(line==null) return 0;
                    var bootstrap=Wire.Object(Wire.Serializer().DeserializeObject(line));
                    checkpoint=Wire.Array(bootstrap["windows"]);
                    Console.WriteLine(Wire.Json(Wire.Obj("ready",true))); Console.Out.Flush();
                    new Driver(CreateRuntime, Wire.Str(bootstrap,"session"), () => workerStore == null ? new object[0] : workerStore.WindowCheckpoint(),
                        () => workerReader != null && workerReader.NeedsRestart).Run();
                } else new Driver((session,mode) => new NativeIsolatedRuntime(session,mode)).Run();
                return 0;
            } catch (Exception e) { Console.Error.WriteLine(e); return 1; }
        }
    }
}

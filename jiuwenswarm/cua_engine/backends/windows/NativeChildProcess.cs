using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;

namespace ComputerUse.Windows {
    // One bounded child, no UIA objects or abandoned native-call threads in
    // the supervising process. Closing the job also contains capture helpers.
    internal sealed class NativeChildProcess : IDisposable {
        [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
            internal long ProcessTime, JobTime; internal uint Flags;
            internal UIntPtr MinWorkingSet, MaxWorkingSet; internal uint ActiveProcesses;
            internal UIntPtr Affinity; internal uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)] struct IoCounters { internal ulong A, B, C, D, E, F; }
        [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
            internal BasicLimits Basic; internal IoCounters Io;
            internal UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string name);
        [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits data, uint size);
        [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        readonly Process process;
        readonly IntPtr job;
        readonly StreamWriter input;
        bool disposed;
        Task<string> pending;
        internal NativeChildProcess(string argument, string executable = null) {
            job = CreateJobObject(IntPtr.Zero, null);
            var limits = new ExtendedLimits(); limits.Basic.Flags = 0x2000; // KILL_ON_JOB_CLOSE
            if (job == IntPtr.Zero || !SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits))) {
                if (job != IntPtr.Zero) CloseHandle(job);
                throw new Failure("observation_unavailable", "cannot create an isolated native worker job");
            }
            process = new Process { StartInfo = new ProcessStartInfo {
                FileName = executable ?? System.Reflection.Assembly.GetExecutingAssembly().Location,
                Arguments = argument, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = new UTF8Encoding(false), StandardErrorEncoding = new UTF8Encoding(false)
            }};
            try {
                process.Start();
                input = new StreamWriter(process.StandardInput.BaseStream, new UTF8Encoding(false)) { AutoFlush=true };
                // The worker waits for its bootstrap frame before doing work.
                if (!AssignProcessToJobObject(job, process.Handle)) throw new IOException("native worker job assignment failed");
                process.ErrorDataReceived += (s,e) => { if (e.Data != null) Console.Error.WriteLine(e.Data); };
                process.BeginErrorReadLine();
            } catch { Dispose(); throw; }
        }
        internal void Send(object value) { input.WriteLine(Wire.Json(value)); }
        internal string Poll(int milliseconds) {
            if (pending == null) pending = process.StandardOutput.ReadLineAsync();
            if (!pending.Wait(milliseconds)) return null;
            string line = pending.GetAwaiter().GetResult(); pending = null;
            if (line == null) throw new IOException("isolated native worker exited");
            if (line.Length > 64 * 1024 * 1024) throw new IOException("isolated native worker frame exceeded its budget");
            return line;
        }
        internal bool Stop(int grace = 0) {
            if (disposed) return true;
            try {
                if (process.HasExited) return true;
                if (input != null) input.Close();
                if (grace > 0 && process.WaitForExit(grace)) return true;
                process.Kill(); return process.WaitForExit(1000);
            } catch (InvalidOperationException) { return true; }
            catch (System.ComponentModel.Win32Exception) { return false; }
            catch (IOException) { return false; }
        }
        public void Dispose() {
            if (disposed) return;
            try { Stop(); } finally { if (job != IntPtr.Zero) CloseHandle(job); process.Dispose(); disposed=true; }
        }
    }
}

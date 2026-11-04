using System;
using System.Collections.Generic;
using System.IO;

namespace ComputerUse.Windows {
    internal sealed class NativeWindowPixels {
        internal string Png;
        internal NativeCaptureGeometry Geometry;
        internal string[] Notes;
        internal static NativeWindowPixels Read(Window window, int maxDimension, Request request) {
            string executable=Path.Combine(Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location),"computer-use-capture.exe");
            if (!File.Exists(executable)) throw new Failure("observation_unavailable","window capture helper is missing; rebuild the Windows driver");
            var failures=new List<string>();
            foreach (string route in new[]{"wgc","print"}) {
                request.Check(); window.Validate();
                try {
                    using(var helper=new NativeChildProcess("",executable)) {
                        helper.Send(Wire.Obj("route",route,"handle",window.Handle.ToInt64(),"pid",window.Pid,"started",window.Started.ToString(),"maxDimension",maxDimension));
                        double deadline=Math.Min(request.Deadline,Wire.Now+(route=="wgc"?2500:1500));
                        string line=null;
                        while (line==null && Wire.Now<deadline) { request.Check(); line=helper.Poll(20); }
                        if(line==null) throw new Failure("observation_unavailable",route+" capture timed out; helper terminated");
                        var result=Wire.Object(Wire.Serializer().DeserializeObject(line));
                        if(result.ContainsKey("error")) throw new Failure("observation_unavailable",(string)result["error"]);
                        window.Validate(); request.Check();
                        var rect=Wire.Object(result["rect"]);
                        var geometry=new NativeCaptureGeometry(Convert.ToInt32(rect["x"]),Convert.ToInt32(rect["y"]),Convert.ToInt32(rect["width"]),Convert.ToInt32(rect["height"]),maxDimension);
                        if(Convert.ToInt32(result["width"])!=geometry.Width || Convert.ToInt32(result["height"])!=geometry.Height)
                            throw new Failure("observation_unavailable","capture helper geometry does not match its pixels");
                        return new NativeWindowPixels {Png=(string)result["png"],Geometry=geometry,
                            Notes=route=="wgc"?new string[0]:new[]{"Compositor capture unavailable ("+String.Join("; ",failures)+"). Returned application-painted PrintWindow pixels; completeness of GPU-composited content is unknown. No activation or screen-region substitution performed."}};
                    }
                } catch(Failure error) {
                    if(error.Code=="cancelled" || error.Code=="deadline_exceeded" || error.Code=="stale_target")throw;
                    failures.Add(error.Message);
                } catch(Exception error) { failures.Add(route+": "+error.Message); }
            }
            throw new Failure("observation_unavailable","window pixel sources unavailable: "+String.Join("; ",failures),"use an explicit desktop screenshot to inspect visible content, or read the target again; no activation performed");
        }
    }
}

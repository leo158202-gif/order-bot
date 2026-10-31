// Trusted launcher: manifest input only, no shell or caller-selected command.
// Build with Framework64 csc; target .NET Framework 4.0, x64.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Web.Script.Serialization;

internal static class AppContainerLauncher {
    [StructLayout(LayoutKind.Sequential)] struct SECURITY_CAPABILITIES { public IntPtr Sid, Capabilities; public int Count, Reserved; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct STARTUPINFO {
        public int cb; public string reserved, desktop, title; public int x,y,xSize,ySize,xChars,yChars,fill,flags; public short show,reserved2; public IntPtr reservedPtr,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct STARTUPINFOEX { public STARTUPINFO si; public IntPtr attrs; }
    [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION { public IntPtr process, thread; public int pid, tid; }
    [StructLayout(LayoutKind.Sequential)] struct BASIC_LIMIT { public long processTime,jobTime; public uint flags; public UIntPtr minWorking,maxWorking; public uint active; public UIntPtr affinity; public uint priority,scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct EXTENDED_LIMIT { public BASIC_LIMIT basic; public IO_COUNTERS io; public UIntPtr processMemory,jobMemory,peakProcess,peakJob; }
    [StructLayout(LayoutKind.Sequential)] struct ACCOUNTING { public long a,b,c,d; public uint faults,total,active,terminated; }
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)] static extern int CreateAppContainerProfile(string name,string display,string description,IntPtr capabilities,int count,out IntPtr sid);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)] static extern int DeleteAppContainerProfile(string name);
    [DllImport("advapi32.dll")] static extern IntPtr FreeSid(IntPtr sid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,int flags,ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr size,IntPtr previous,IntPtr returned);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcess(string app,StringBuilder cmd,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref STARTUPINFOEX startup,out PROCESS_INFORMATION info);
    [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int kind,ref EXTENDED_LIMIT limits,int length);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int kind,out ACCOUNTING accounting,int length,IntPtr returned);
    [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);

    static void Check(bool ok,string action) { if(!ok) throw new Win32Exception(Marshal.GetLastWin32Error(),action); }
    static string Required(Dictionary<string,object> m,string key) { return (string)m[key]; }
    static string Under(string root,string value) {
        string p=Path.GetFullPath(value), r=Path.GetFullPath(root).TrimEnd('\\')+"\\";
        if(!p.StartsWith(r,StringComparison.OrdinalIgnoreCase)) throw new Exception("path outside task root");
        string cursor=p;
        while(cursor!=null) { if((File.Exists(cursor)||Directory.Exists(cursor)) && (File.GetAttributes(cursor)&FileAttributes.ReparsePoint)!=0) throw new Exception("reparse point refused"); cursor=Path.GetDirectoryName(cursor); }
        return p;
    }
    static void Grant(string path,SecurityIdentifier sid,FileSystemRights rights,bool recursive) {
        if(Directory.Exists(path)) {
            var security=Directory.GetAccessControl(path);
            security.AddAccessRule(new FileSystemAccessRule(sid,rights,recursive ? InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit : InheritanceFlags.None,PropagationFlags.None,AccessControlType.Allow));
            Directory.SetAccessControl(path,security);
        } else {
            var security=File.GetAccessControl(path); security.AddAccessRule(new FileSystemAccessRule(sid,rights,AccessControlType.Allow)); File.SetAccessControl(path,security);
        }
    }
    static string Quote(string s) { if(s.IndexOf('"')>=0 || s.EndsWith("\\")) throw new Exception("bad command path"); return "\""+s+"\""; }
    public static int Main(string[] args) {
        IntPtr sid=IntPtr.Zero,attrs=IntPtr.Zero,capsMem=IntPtr.Zero,env=IntPtr.Zero,job=IntPtr.Zero;
        PROCESS_INFORMATION pi=new PROCESS_INFORMATION(); bool assigned=false; string profile="OrderBot."+Guid.NewGuid().ToString("N");
        var result=new Dictionary<string,object>();
        try {
            if(args.Length!=1) throw new Exception("one manifest path required");
            string manifest=Path.GetFullPath(args[0]);
            if(new FileInfo(manifest).Length>32768) throw new Exception("manifest too large");
            var m=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(manifest,Encoding.UTF8));
            string root=Path.GetFullPath(Required(m,"task_root"));
            Under(root,manifest);
            string runtime=Under(root,Required(m,"runtime"));
            string code=Under(root,Required(m,"code")), input=Under(root,Required(m,"input")), output=Under(root,Required(m,"output"));
            string python=Under(runtime,Path.Combine(runtime,"python.exe"));
            string entry=Under(code,Path.Combine(code,"entry.py"));
            if(Path.GetFileName(output)!="output" || !Directory.Exists(output)) throw new Exception("fixed output directory required");
            int timeout=Convert.ToInt32(m["timeout_ms"]),memory=Convert.ToInt32(m["memory_mb"]);
            if(timeout<100 || timeout>120000 || memory<64 || memory>1024) throw new Exception("limits outside bounds");
            int hr=CreateAppContainerProfile(profile,profile,"OrderBot isolated business worker",IntPtr.Zero,0,out sid);
            if(hr!=0) Marshal.ThrowExceptionForHR(hr);
            var identity=new SecurityIdentifier(sid);
            Grant(root,identity,FileSystemRights.Traverse|FileSystemRights.ReadAttributes,false);
            Grant(runtime,identity,FileSystemRights.ReadAndExecute,true);
            Grant(code,identity,FileSystemRights.ReadAndExecute,true);
            Grant(input,identity,FileSystemRights.Read,true);
            Grant(output,identity,FileSystemRights.Modify,true);
            // Every execution receives a fresh SID, and no network capability SID.
            IntPtr bytes=IntPtr.Zero; InitializeProcThreadAttributeList(IntPtr.Zero,1,0,ref bytes);
            attrs=Marshal.AllocHGlobal(bytes); Check(InitializeProcThreadAttributeList(attrs,1,0,ref bytes),"attribute list");
            SECURITY_CAPABILITIES caps=new SECURITY_CAPABILITIES(); caps.Sid=sid;
            capsMem=Marshal.AllocHGlobal(Marshal.SizeOf(caps)); Marshal.StructureToPtr(caps,capsMem,false);
            Check(UpdateProcThreadAttribute(attrs,0,(IntPtr)0x20009,capsMem,(IntPtr)Marshal.SizeOf(caps),IntPtr.Zero,IntPtr.Zero),"security capabilities");
            job=CreateJobObject(IntPtr.Zero,null); Check(job!=IntPtr.Zero,"job object");
            EXTENDED_LIMIT limits=new EXTENDED_LIMIT();
            limits.basic.flags=0x2000|0x8|0x100|0x200; // kill-on-close, one process, process and job memory
            limits.basic.active=1; limits.processMemory=(UIntPtr)((ulong)memory*1024*1024); limits.jobMemory=limits.processMemory;
            Check(SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(limits)),"job limits");
            string windir=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            string environment="LOCALAPPDATA="+Environment.GetEnvironmentVariable("LOCALAPPDATA")+"\0PATH="+runtime+"\0SystemRoot="+windir+"\0TEMP="+output+"\0TMP="+output+"\0WINDIR="+windir+"\0\0";
            env=Marshal.StringToHGlobalUni(environment);
            STARTUPINFOEX si=new STARTUPINFOEX(); si.si.cb=Marshal.SizeOf(si); si.attrs=attrs;
            var command=new StringBuilder(Quote(python)+" -I -S -B "+Quote(entry)+" "+Quote(input)+" "+Quote(Path.Combine(output,"result.json")));
            Check(CreateProcess(python,command,IntPtr.Zero,IntPtr.Zero,false,0x80000|0x4|0x400|0x8000000,env,output,ref si,out pi),"create restricted process");
            Check(AssignProcessToJobObject(job,pi.process),"assign suspended worker to job"); assigned=true;
            Check(ResumeThread(pi.thread)!=0xffffffff,"resume worker");
            bool timed=WaitForSingleObject(pi.process,(uint)timeout)==258;
            if(timed) { Check(TerminateJobObject(job,124),"terminate timed-out job"); WaitForSingleObject(pi.process,5000); }
            uint exit; GetExitCodeProcess(pi.process,out exit);
            ACCOUNTING accounting; Check(QueryInformationJobObject(job,1,out accounting,Marshal.SizeOf(typeof(ACCOUNTING)),IntPtr.Zero),"query job");
            for(int retry=0; accounting.active!=0 && retry<100; retry++) {
                System.Threading.Thread.Sleep(10);
                Check(QueryInformationJobObject(job,1,out accounting,Marshal.SizeOf(typeof(ACCOUNTING)),IntPtr.Zero),"query completed job");
            }
            result["status"]=timed ? "timeout" : "finished"; result["exit_code"]=exit;
            result["pid"]=pi.pid; result["active_processes"]=accounting.active;
            result["appcontainer"]=true; result["network_capabilities"]=0; result["process_limit"]=1;
        } catch(Exception e) { result["status"]="failed"; result["error"]=e.Message; if(e is Win32Exception) result["error"]=e.Message+" (Win32 "+((Win32Exception)e).NativeErrorCode+")"; }
        finally {
            if(pi.process!=IntPtr.Zero && !assigned) TerminateProcess(pi.process,125);
            if(job!=IntPtr.Zero) { TerminateJobObject(job,125); CloseHandle(job); }
            if(pi.thread!=IntPtr.Zero) CloseHandle(pi.thread); if(pi.process!=IntPtr.Zero) CloseHandle(pi.process);
            if(attrs!=IntPtr.Zero) { DeleteProcThreadAttributeList(attrs); Marshal.FreeHGlobal(attrs); }
            if(capsMem!=IntPtr.Zero) Marshal.FreeHGlobal(capsMem); if(env!=IntPtr.Zero) Marshal.FreeHGlobal(env);
            if(sid!=IntPtr.Zero) { FreeSid(sid); DeleteAppContainerProfile(profile); }
        }
        Console.WriteLine(new JavaScriptSerializer().Serialize(result));
        return result.ContainsKey("error") ? 1 : 0;
    }
}

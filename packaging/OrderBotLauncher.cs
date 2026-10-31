using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows.Forms;
class OrderBotLauncher {
  [STAThread] static int Main(string[] args) {
    bool created;
    using (Mutex singleton = new Mutex(true, "Global\\OrderBotPackageLauncher", out created)) {
      if (!created) { MessageBox.Show("OrderBot 已在运行，请使用现有窗口。", "OrderBot"); return 2; }
      try {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        using (FileStream running = new FileStream(Path.Combine(root, "launcher.running"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None)) {
        string runtime = Path.Combine(root, "runtime", "pythonw.exe");
        string script = Path.Combine(root, "bootstrap.py");
        if (!File.Exists(runtime) || !File.Exists(script)) throw new IOException("运行组件不完整，请重新安装程序；客户数据会保留。");
        ProcessStartInfo start = new ProcessStartInfo(runtime, "-I -B \"" + script + "\"");
        start.WorkingDirectory = root; start.UseShellExecute = false; start.CreateNoWindow = true;
        start.EnvironmentVariables["ORDER_BOT_INSTALL_ROOT"] = root;
        start.EnvironmentVariables["ORDER_BOT_DATA_DIR"] = Path.Combine(root, "data");
        using (Process child = Process.Start(start)) { child.WaitForExit(); return child.ExitCode; }
        }
      } catch (Exception ex) { MessageBox.Show(ex.Message, "OrderBot 启动未完成"); return 1; }
      finally { singleton.ReleaseMutex(); }
    }
  }
}

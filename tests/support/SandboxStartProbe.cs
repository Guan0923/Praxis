using System;
using System.ServiceProcess;

public sealed class SandboxStartProbe : ServiceBase
{
    public SandboxStartProbe(string name) { ServiceName = name; AutoLog = false; }
    protected override void OnStart(string[] args) { }
    protected override void OnStop() { }
    public static void Main(string[] args) { Run(new SandboxStartProbe(args[0])); }
}

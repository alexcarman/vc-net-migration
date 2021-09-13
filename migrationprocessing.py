#!/usr/bin/env python

from __future__ import print_function
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from tqdm import tqdm
from dataclasses import dataclass
import atexit
import sys
import ssl
import csv
import argparse
import getpass


@dataclass
class VmInfo:  
    def __init__(self, name, cluster, vSwitch, pg, vlanId, vmhost):  
        self.name = name
        self.cluster = cluster
        self.vswitch = vSwitch
        self.pg = pg
        self.vlanId = vlanId
        self.vmhost = vmhost


@dataclass
class PortGroup:
    def __init__(self, name, vlanId):
        self.name = name
        self.vlanId = vlanId
    
    def __eq__(self, other):
        return self.name == other.name

    def __hash__(self):
        return hash(self.name)


def get_obj(content, vimtype, name):
    """
     Get the vsphere object associated with a given text name
    """
    obj = None
    container = content.viewManager.CreateContainerView(content.rootFolder,
                                                        vimtype, True)
    for view in container.view:
        if view.name == name:
            obj = view
            break
    return obj


def GetVMHosts(content, cluster):
    print("Getting list of ESX hosts for " + cluster + "  ...")
    host_view = content.viewManager.CreateContainerView(content.rootFolder,
                                                        [vim.HostSystem],
                                                        True)
    obj = [host for host in tqdm(host_view.view) if host.parent.name == cluster]
    host_view.Destroy()
    return obj


def GetVMs(content, cluster):
    print("Getting list of VMs for " + cluster + " ...")
    vm_view = content.viewManager.CreateContainerView(content.rootFolder,
                                                      [vim.VirtualMachine],
                                                      True)
    obj = [vm for vm in tqdm(vm_view.view) if vm.summary.runtime.host.parent.name == cluster]
    vm_view.Destroy()
    return obj


def GetDVSPG(content):
    dvs_list = []
    print("Getting All DVS ...")
    dvs_view = content.viewManager.CreateContainerView(content.rootFolder,
                                                       [vim.DistributedVirtualSwitch],
                                                       True)
    print("Getting all DVS portgroups ...")
    for dvs in dvs_view.view:
        for dvs_pg in tqdm(dvs.portgroup):
            dvs_list.append(dvs_pg.name)
    return dvs_list


def GetHostsPortgroups(hosts):
    print("Collecting portgroups on all hosts. This may take a while.")
    hostPgDict = {}
    for host in tqdm(hosts):
        pgs = host.config.network.portgroup
        hostPgDict[host] = pgs
    print("Portgroup collection complete.")
    return hostPgDict


def GetVMInfo(vm):
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualEthernetCard):
            dev_backing = dev.backing
            portGroup = None
            vlanId = None
            vSwitch = None
            if hasattr(dev_backing, 'port'):
                portGroupKey = dev.backing.port.portgroupKey
                dvsUuid = dev.backing.port.switchUuid
                try:
                    dvs = content.dvSwitchManager.QueryDvsByUuid(dvsUuid)
                except:
                    portGroup = "** Error: DVS not found **"
                    vlanId = "NA"
                    vSwitch = "NA"
                else:
                    pgObj = dvs.LookupDvPortGroup(portGroupKey)
                    portGroup = pgObj.config.name
                    vlanId = str(pgObj.config.defaultPortConfig.vlan.vlanId)
                    vSwitch = str(dvs.name)
            else:
                portGroup = dev.backing.network.name
                vmHost = vm.runtime.host
                # global variable hosts is a list, not a dict
                host_pos = hosts.index(vmHost)
                viewHost = hosts[host_pos]
                # global variable hostPgDict stores portgroups per host
                pgs = hostPgDict[viewHost]
                for p in pgs:
                    if portGroup in p.key:
                        vlanId = str(p.spec.vlanId)
                        vSwitch = str(p.spec.vswitchName)
            if not portGroup:
                portGroup = 'NA'
            if not vlanId:
                vlanId = 'NA'
            if not vSwitch:
                vSwitch = 'NA'
            return VmInfo(vm.name, vm.summary.runtime.host.parent.name, vSwitch, portGroup, vlanId, vm.runtime.host)


def GetArgs():
    """Get command line args from the user.
    """

    parser = argparse.ArgumentParser(
        description='Pull VM DVS Assignments')

    parser.add_argument('-t', '--host',
                        action='store',
                        help='vSphere server to connect')

    parser.add_argument('-o', '--port',
                        type=int,
                        default=443,
                        action='store',
                        help='Port to connect on')

    parser.add_argument('-u', '--user',
                        action='store',
                        help='Username to use')

    parser.add_argument('-p', '--password',
                        action='store',
                        help='Password to use')
    
    parser.add_argument('-c', '--command',
                        action='store',
                        help='Command to run(createvswitch, createportgroups, migratetovswitch, migratetodvs')

    parser.add_argument('-s', '--cluster',
                        action='store',
                        help='The cluster to perform a migration on')

    parser.add_argument('-v', '--vswitch',
                        action='store',
                        help='vSwitch to create')

    args = parser.parse_args()

    if not args.host:
        args.host = input("vCenter Host: ")
    if not args.user:
        args.user = input("Username: ")
    if not args.password:
        args.password = getpass.getpass('Password: ')
    if not args.command:
        args.command = input("Command: ")
    if not args.cluster:
        args.cluster = input("Cluster: ")
    return args


def AddHostsSwitch(hosts, vswitchName):
    if not vswitchName:
        vswitchName = input("New vSwitch Name: ")
    print("Adding vswitches, please wait ... ")
    for host in tqdm(hosts):
        AddHostSwitch(host, vswitchName)


def AddHostSwitch(host, vswitchName):
    vswitch_spec = vim.host.VirtualSwitch.Specification()
    vswitch_spec.numPorts = 1024
    vswitch_spec.mtu = 1500
    vswitch_spec.bridge = vim.host.VirtualSwitch.BondBridge(nicDevice=["vmnic7"])
    host.configManager.networkSystem.AddVirtualSwitch(vswitchName,
                                                      vswitch_spec)

def AddHostsPortGroups(hosts, vswitch, vms):
    pgstocreate = set()
    if not vswitch:
        vswitch = input("New vSwitch Name: ")
    print("Compiling list of Port Groups to create. Please wait longer.")
    #pgstocreate = {PortGroup(vm.pg.split('|')[2], vm.vlanId) for vm in tqdm(vms) if "vSwitch" not in vm.vswitch}
    for vm in tqdm(vms):
        vminfo = GetVMInfo(vm)
        if "vSwitch" not in vminfo.vswitch:
            pgstocreate.add(PortGroup(vminfo.pg.split('|')[2], vminfo.vlanId))
    
    print("Creating " + str(len(pgstocreate)) + " port groups!")
    for pg in tqdm(pgstocreate):
        for host in hosts:
            AddHostPortgroup(host, vswitch, pg.name, pg.vlanId)


def AddHostPortgroup(host, vswitchName, portgroupName, vlanId):
    portgroup_spec = vim.host.PortGroup.Specification()
    portgroup_spec.vswitchName = vswitchName
    portgroup_spec.name = portgroupName
    portgroup_spec.vlanId = int(vlanId)
    network_policy = vim.host.NetworkPolicy()
    network_policy.security = vim.host.NetworkPolicy.SecurityPolicy()
    network_policy.security.allowPromiscuous = True
    network_policy.security.macChanges = False
    network_policy.security.forgedTransmits = False
    portgroup_spec.policy = network_policy

    host.configManager.networkSystem.AddPortGroup(portgroup_spec)


def MigrateToVswitch(vms):
    print("Migrating VMs from DVS to standard Vswitch ... ")
    failedVMs = []
    for vm in tqdm(vms):
        vminfo = GetVMInfo(vm)
        if "VM Network" not in vminfo.pg:
            try:
                ChangeVmVif(vm, False, vminfo.pg.split('|')[2])
            except IndexError:
                failedVMs.append(vminfo.name)
                pass
            continue
    print("These VMs failed to migrate: ")
    print(failedVMs)


def MigrateToDvs(vms):
    dvs_list = GetDVSPG(content)
    print("Migrating VMs from standard Vswitch to DVS ... ")
    for vm in tqdm(vms):
        vminfo = GetVMInfo(vm)
        if "VM Network" not in vminfo.pg:
            dvs_set = [ dvs for dvs in dvs_list if vminfo.pg == dvs.split('|')[2] ]
            ChangeVmVif(vm, True, dvs_set[0])


def ChangeVmVif(vm, is_VDS, pg):
    """This will change the network port group on the vm that is passed in"""
    device_change = []
    for device in vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualEthernetCard):
            nicspec = vim.vm.device.VirtualDeviceSpec()
            nicspec.operation = \
                vim.vm.device.VirtualDeviceSpec.Operation.edit
            nicspec.device = device
            nicspec.device.wakeOnLanEnabled = True

            if not is_VDS:
                nicspec.device.backing = \
                    vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
                nicspec.device.backing.network = \
                    get_obj(content, [vim.Network], pg)
                nicspec.device.backing.deviceName = pg
            else:
                network = get_obj(content,
                                    [vim.dvs.DistributedVirtualPortgroup],
                                    pg)


                dvs_port_connection = vim.dvs.PortConnection()
                dvs_port_connection.portgroupKey = network.key
                dvs_port_connection.switchUuid = \
                    network.config.distributedVirtualSwitch.uuid
                nicspec.device.backing = \
                    vim.vm.device.VirtualEthernetCard. \
                    DistributedVirtualPortBackingInfo()
                nicspec.device.backing.port = dvs_port_connection

            nicspec.device.connectable = \
                vim.vm.device.VirtualDevice.ConnectInfo()
            nicspec.device.connectable.connected = True
            nicspec.device.connectable.startConnected = True
            nicspec.device.connectable.allowGuestControl = True
            device_change.append(nicspec)

    config_spec = vim.vm.ConfigSpec(deviceChange=device_change)
    waitForTask(vm.ReconfigVM_Task(config_spec))


def waitForTask(task):
    """ wait for a vCenter task to finish """
    task_done = False
    while not task_done:
        if task.info.state == 'success':
            return task.info.result

        if task.info.state == 'error':
            print(task.info.error.msg)
            task_done = True


def main():
    global content, hosts, hostPgDict
    args= GetArgs()
    vminfos = []
    pgstocreate = {}

    default_context = ssl._create_default_https_context
    ssl._create_default_https_context = ssl._create_unverified_context
    
    serviceInstance = SmartConnect(host=args.host,
                                   user=args.user,
                                   pwd=args.password,
                                   port=args.port)
    atexit.register(Disconnect, serviceInstance)
    #Get our stuff from vcenter
    content = serviceInstance.RetrieveContent()
    hosts = GetVMHosts(content, args.cluster)
    hostPgDict = GetHostsPortgroups(hosts)
    vms = GetVMs(content, args.cluster)
    #This is ugly because python doesn't have a switch, maybe I'll learn a more pythonic way.
    if args.command == "createvswitch":
        AddHostsSwitch(hosts, args.vswitch)
    elif args.command == "createportgroups":
        AddHostsPortGroups(hosts, args.vswitch, vms)
    elif args.command == "migratetovswitch":
        MigrateToVswitch(vms)
    elif args.command == "migratetodvs":
        MigrateToDvs(vms)
    else:
        print("Nothing to do")


# Main section
if __name__ == "__main__":
    sys.exit(main())

# vc-net-migration
This is a small python project I developed to assist in migrating a massive number of VMs from one version of vcenter to another. When moving VMs between vcenter servers they cannot be on a DVS, this presented a challenge for me. I didn't want to create every standard vswitch, edit every vm, and then change them back after the migration.

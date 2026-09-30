#!/bin/bash
# Create a Proxmox VE test VM (q35, OVMF/UEFI) that boots a CouchLiteOS ISO.
# Run on the Proxmox host as root after uploading the ISO to an ISO storage.
# The VM covers boot, launcher, Settings, persistence, and Remote Desktop; it
# cannot emulate the iMac's Kepler GPU, Broadcom Wi-Fi, or audio.
# The script never replaces or deletes an existing VM.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
usage: proxmox-create-test-vm.sh --iso STORAGE:iso/FILE.iso [options]
  --vmid ID            VM id (default: next free id)
  --name NAME          VM name (default: couchliteos-test)
  --storage STORAGE    disk and EFI storage (default: local-lvm)
  --bridge BRIDGE      network bridge (default: vmbr0)
  --memory MIB         memory (default: 4096)
  --cores N            CPU cores (default: 4)
  --disk GIB           install-target disk size (default: 32)
  --secure-boot        enroll Microsoft keys (the ISO ships Debian's signed shim)
  --start              start the VM after creating it
EOF
  exit 64
}

iso= vmid= name=couchliteos-test storage=local-lvm bridge=vmbr0 memory=4096 cores=4 disk=32 keys=0 start=0
while (($#)); do
  case $1 in
    --iso) iso=${2:-}; shift 2 ;;
    --vmid) vmid=${2:-}; shift 2 ;;
    --name) name=${2:-}; shift 2 ;;
    --storage) storage=${2:-}; shift 2 ;;
    --bridge) bridge=${2:-}; shift 2 ;;
    --memory) memory=${2:-}; shift 2 ;;
    --cores) cores=${2:-}; shift 2 ;;
    --disk) disk=${2:-}; shift 2 ;;
    --secure-boot) keys=1; shift ;;
    --start) start=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

command -v qm >/dev/null && command -v pvesm >/dev/null || { echo 'Run this on a Proxmox VE host.' >&2; exit 69; }
[[ $iso =~ ^[A-Za-z0-9_.-]+:iso/[A-Za-z0-9_.+-]+\.iso$ ]] || { echo 'Give --iso as STORAGE:iso/FILE.iso' >&2; usage; }
pvesm path "$iso" >/dev/null || { echo "ISO volume not found: $iso" >&2; exit 66; }
[[ $name =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo 'invalid --name' >&2; exit 64; }
for value in "$memory" "$cores" "$disk"; do [[ $value =~ ^[1-9][0-9]*$ ]] || usage; done
vmid=${vmid:-$(pvesh get /cluster/nextid)}
[[ $vmid =~ ^[1-9][0-9]*$ ]] || usage
if qm status "$vmid" >/dev/null 2>&1; then
  echo "VM $vmid already exists; choose another --vmid. This script never replaces VMs." >&2
  exit 73
fi

qm create "$vmid" \
  --name "$name" \
  --machine q35 \
  --bios ovmf \
  --ostype l26 \
  --cpu host \
  --cores "$cores" \
  --memory "$memory" \
  --balloon 0 \
  --scsihw virtio-scsi-single \
  --net0 "virtio,bridge=$bridge" \
  --vga virtio \
  --serial0 socket \
  --tablet 1
qm set "$vmid" --efidisk0 "$storage:1,efitype=4m,pre-enrolled-keys=$keys"
qm set "$vmid" --scsi0 "$storage:$disk,discard=on,ssd=1,iothread=1"
qm set "$vmid" --ide2 "$iso,media=cdrom"
qm set "$vmid" --boot 'order=ide2;scsi0'

echo "Created VM $vmid ($name) from $iso"
echo "  UEFI (OVMF), Secure Boot keys enrolled: $([[ $keys == 1 ]] && echo yes || echo no)"
echo "  ${disk} GiB install-target disk on $storage; network on $bridge"
echo "  Serial console: qm terminal $vmid"
if ((start)); then
  qm start "$vmid"
  echo "Started. Open the noVNC console in the Proxmox web UI."
fi
echo "After installing, detach the ISO with: qm set $vmid --ide2 none,media=cdrom"

#!/usr/bin/env python3
# 
# Cross Platform and Multi Architecture Advanced Binary Emulation Framework
#

"""
This module is intended for general purpose functions that are only used in qiling.os
"""

from typing import Any, MutableMapping, Mapping, Union, Sequence, MutableSequence, Tuple
from uuid import UUID
import ctypes

from unicorn import UcError

from qiling import Qiling
from qiling.const import QL_VERBOSE
from qiling.os.const import POINTER
from qiling.os.windows.fncc import STDCALL
from qiling.os.windows.wdk_const import *
from qiling.os.windows.structs import *
from qiling.utils import verify_ret

class QlOsUtils:

    ELLIPSIS_PREF = r'__qlva_'

    def __init__(self, ql: Qiling):
        self.ql = ql

        # We can save every syscall called
        self.syscalls = {}
        self.syscalls_counter = 0
        self.appeared_strings = {}

    def clear_syscalls(self):
        """Reset API and string appearance stats.
        """

        self.syscalls = {}
        self.syscalls_counter = 0
        self.appeared_strings = {}

    def _call_api(self, address: int, name: str, params: Mapping, retval: Any, retaddr: int) -> None:
        """Record API calls along with their details.

        Args:
            address : location of the calling instruction
            name    : api function name
            params  : mapping of the parameters name to their effective values
            retval  : value returned by the api function
            retaddr : address to which the api function returned
        """

        if name.startswith('hook_'):
            name = name[5:]

        self.syscalls.setdefault(name, []).append({
            'params': params,
            'retval': retval,
            'address': address,
            'retaddr': retaddr,
            'position': self.syscalls_counter
        })

        self.syscalls_counter += 1

    def string_appearance(self, s: str) -> None:
        """Record strings appearance as they are encountered during emulation.

        Args:
            s : string to record
        """

        for token in s.split(' '):
            self.appeared_strings.setdefault(token, set()).add(self.syscalls_counter)

    @staticmethod
    def read_string(ql: Qiling, address: int, terminator: str) -> str:
        result = ""
        charlen = len(terminator)

        char = ql.mem.read(address, charlen)

        while char.decode(errors="ignore") != terminator:
            address += charlen
            result += char.decode(errors="ignore")
            char = ql.mem.read(address, charlen)

        return result

    def read_wstring(self, address: int) -> str:
        s = QlOsUtils.read_string(self.ql, address, '\x00\x00')

        # We need to remove \x00 inside the string. Compares do not work otherwise
        s = s.replace("\x00", "")
        self.string_appearance(s)

        return s

    def read_cstring(self, address: int) -> str:
        s = QlOsUtils.read_string(self.ql, address, '\x00')

        self.string_appearance(s)

        return s

    def read_guid(self, address: int) -> UUID:
        raw_guid = self.ql.mem.read(address, 16)

        return UUID(bytes_le=bytes(raw_guid))

    @staticmethod
    def stringify(s: str) -> str:
        """Decorate a string with quotation marks.
        """

        return f'"{repr(s)[1:-1]}"'

    def print_function(self, address: int, fname: str, pargs: Sequence[Tuple[str, str]], ret: Union[int, str, None], passthru: bool):
        '''Print out function invocation detais.

        Args:
            address: fucntion address
            fnamr: function name
            pargs: processed args list: a sequence of 2-tuples consisting of arg names paired to string representation of arg values
            ret: function return value, or None if no such value
            passthru: whether this is a passthrough invocation (no frame unwinding)
        '''

        if fname.startswith('hook_'):
            fname = fname[5:]

        def __assign_arg(name: str, value: str) -> str:
            # ignore arg names generated by variadric functions
            if name.startswith(QlOsUtils.ELLIPSIS_PREF):
                name = ''

            return f'{name} = {value}' if name else f'{value}'

        # arguments list
        fargs = ', '.join(__assign_arg(name, value) for name, value in pargs)

        if type(ret) is int:
            ret = f'{ret:#x}'

        # optional prefixes and suffixes
        fret = f' = {ret}' if ret is not None else ''
        fpass = f' (PASSTHRU)' if passthru else ''
        faddr = f'{address:#0{self.ql.archbit // 4 + 2}x}: ' if self.ql.verbose >= QL_VERBOSE.DEBUG else ''

        log = f'{faddr}{fname}({fargs}){fret}{fpass}'

        if self.ql.verbose >= QL_VERBOSE.DEBUG:
            self.ql.log.debug(log)
        else:
            self.ql.log.info(log)

    def __common_printf(self, format: str, args: MutableSequence, wstring: bool):
        fmtstr = format.split("%")[1:]
        read_string = self.read_wstring if wstring else self.read_cstring

        for i, f in enumerate(fmtstr):
            if f.startswith("s"):
                args[i] = read_string(args[i])

        out = format.replace(r'%llx', r'%x')
        out = out.replace(r'%p', r'%#x')

        return out % tuple(args)

    def va_list(self, format: str, ptr: int) -> MutableSequence[int]:
        count = format.count("%")

        return [self.ql.unpack(self.ql.mem.read(ptr + i * self.ql.pointersize, self.ql.pointersize)) for i in range(count)]

    def sprintf(self, buff: int, format: str, args: MutableSequence, wstring: bool = False) -> int:
        out = self.__common_printf(format, args, wstring)
        enc = 'utf-16le' if wstring else 'utf-8'

        self.ql.mem.write(buff, (out + '\x00').encode(enc))

        return len(out)

    def printf(self, format: str, args: MutableSequence, wstring: bool = False) -> int:
        out = self.__common_printf(format, args, wstring)
        enc = 'utf-8'

        self.ql.os.stdout.write(out.encode(enc))

        return len(out)

    def update_ellipsis(self, params: MutableMapping, args: Sequence) -> None:
        params.update((f'{QlOsUtils.ELLIPSIS_PREF}{i}', a) for i, a in enumerate(args))

    def exec_arbitrary(self, start: int, end: int):
        old_sp = self.ql.reg.arch_sp

        # we read where this hook is supposed to return
        ret = self.ql.stack_read(0)

        def restore(ql: Qiling):
            self.ql.log.debug(f"Executed code from {start:#x} to {end:#x}")
            # now we can restore the register to be where we were supposed to
            ql.reg.arch_sp = old_sp + ql.pointersize
            ql.reg.arch_pc = ret

            # we want to execute the code once, not more
            hret.remove()

        # we have to set an address to restore the registers
        hret = self.ql.hook_address(restore, end)
        # we want to rewrite the return address to the function
        self.ql.stack_write(0, start)

    def io_Write(self, in_buffer):
        heap = self.ql.os.heap

        if self.ql.ostype == QL_OS.WINDOWS:

            if self.ql.loader.driver_object.MajorFunction[IRP_MJ_WRITE] == 0:
                # raise error?
                return (False, None)

        if self.ql.archbit == 32:
            buf = self.ql.mem.read(self.ql.loader.driver_object.DeviceObject, ctypes.sizeof(DEVICE_OBJECT32))
            device_object = DEVICE_OBJECT32.from_buffer(buf)
        else:
            buf = self.ql.mem.read(self.ql.loader.driver_object.DeviceObject, ctypes.sizeof(DEVICE_OBJECT64))
            device_object = DEVICE_OBJECT64.from_buffer(buf)

        alloc_addr = []
        def build_mdl(buffer_size, data=None):
            if self.ql.archtype == QL_ARCH.X8664:
                mdl = MDL64()
            else:
                mdl = MDL32()

            mapped_address = heap.alloc(buffer_size)
            alloc_addr.append(mapped_address)
            mdl.MappedSystemVa.value = mapped_address
            mdl.StartVa.value = mapped_address
            mdl.ByteOffset = 0
            mdl.ByteCount = buffer_size
            if data:
                written = data if len(data) <= buffer_size else data[:buffer_size]
                self.ql.mem.write(mapped_address, written)

            return mdl
        # allocate memory regions for IRP and IO_STACK_LOCATION
        if self.ql.archtype == QL_ARCH.X8664:
            irp_addr = heap.alloc(ctypes.sizeof(IRP64))
            alloc_addr.append(irp_addr)
            irpstack_addr = heap.alloc(ctypes.sizeof(IO_STACK_LOCATION64))
            alloc_addr.append(irpstack_addr)
            # setup irp stack parameters
            irpstack = IO_STACK_LOCATION64()
            # setup IRP structure
            irp = IRP64()
            irp.irpstack = ctypes.cast(irpstack_addr, ctypes.POINTER(IO_STACK_LOCATION64))
        else:
            irp_addr = heap.alloc(ctypes.sizeof(IRP32))
            alloc_addr.append(irp_addr)
            irpstack_addr = heap.alloc(ctypes.sizeof(IO_STACK_LOCATION32))
            alloc_addr.append(irpstack_addr)
            # setup irp stack parameters
            irpstack = IO_STACK_LOCATION32()
            # setup IRP structure
            irp = IRP32()
            irp.irpstack = ctypes.cast(irpstack_addr, ctypes.POINTER(IO_STACK_LOCATION32))

        irpstack.MajorFunction = IRP_MJ_WRITE
        irpstack.Parameters.Write.Length = len(in_buffer)
        self.ql.mem.write(irpstack_addr, bytes(irpstack))

        if device_object.Flags & DO_BUFFERED_IO:
            # BUFFERED_IO
            system_buffer_addr = heap.alloc(len(in_buffer))
            alloc_addr.append(system_buffer_addr)
            self.ql.mem.write(system_buffer_addr, bytes(in_buffer))
            irp.AssociatedIrp.SystemBuffer.value = system_buffer_addr
        elif device_object.Flags & DO_DIRECT_IO:
            # DIRECT_IO
            mdl = build_mdl(len(in_buffer))
            if self.ql.archtype == QL_ARCH.X8664:
                mdl_addr = heap.alloc(ctypes.sizeof(MDL64))
            else:
                mdl_addr = heap.alloc(ctypes.sizeof(MDL32))

            alloc_addr.append(mdl_addr)

            self.ql.mem.write(mdl_addr, bytes(mdl))
            irp.MdlAddress.value = mdl_addr
        else:
            # NEITHER_IO
            input_buffer_size = len(in_buffer)
            input_buffer_addr = heap.alloc(input_buffer_size)
            alloc_addr.append(input_buffer_addr)
            self.ql.mem.write(input_buffer_addr, bytes(in_buffer))
            irp.UserBuffer.value = input_buffer_addr

        # everything is done! Write IRP to memory
        self.ql.mem.write(irp_addr, bytes(irp))

        # set function args
        # TODO: make sure this is indeed STDCALL
        self.ql.os.fcall = self.ql.os.fcall_select(STDCALL)
        self.ql.os.fcall.writeParams(((POINTER, self.ql.loader.driver_object.DeviceObject), (POINTER, irp_addr)))

        try:
            # now emulate 
            self.ql.run(self.ql.loader.driver_object.MajorFunction[IRP_MJ_WRITE])
        except UcError as err:
            verify_ret(self.ql, err)
            
        # read current IRP state
        if self.ql.archtype == QL_ARCH.X8664:
            irp_buffer = self.ql.mem.read(irp_addr, ctypes.sizeof(IRP64))
            irp = IRP64.from_buffer(irp_buffer)
        else:
            irp_buffer = self.ql.mem.read(irp_addr, ctypes.sizeof(IRP32))
            irp = IRP32.from_buffer(irp_buffer)

        io_status = irp.IoStatus
        # now free all alloc memory
        for addr in alloc_addr:
            # print("freeing heap memory at 0x%x" %addr) # FIXME: the output is not deterministic??
            heap.free(addr)
        return True, io_status.Information.value

    # Emulate DeviceIoControl() of Windows
    # BOOL DeviceIoControl(
    #      HANDLE       hDevice,
    #      DWORD        dwIoControlCode,
    #      LPVOID       lpInBuffer,
    #      DWORD        nInBufferSize,
    #      LPVOID       lpOutBuffer,
    #      DWORD        nOutBufferSize,
    #      LPDWORD      lpBytesReturned,
    #      LPOVERLAPPED lpOverlapped);
    def ioctl(self, params):
        heap = self.ql.os.heap

        def ioctl_code(DeviceType, Function, Method, Access):
            return (DeviceType << 16) | (Access << 14) | (Function << 2) | Method

        alloc_addr = []
        def build_mdl(buffer_size, data=None):
            if self.ql.archtype == QL_ARCH.X8664:
                mdl = MDL64()
            else:
                mdl = MDL32()

            mapped_address = heap.alloc(buffer_size)
            alloc_addr.append(mapped_address)
            mdl.MappedSystemVa.value = mapped_address
            mdl.StartVa.value = mapped_address
            mdl.ByteOffset = 0
            mdl.ByteCount = buffer_size
            if data:
                written = data if len(data) <= buffer_size else data[:buffer_size]
                self.ql.mem.write(mapped_address, written)

            return mdl

        # quick simple way to manage all alloc memory
        if self.ql.ostype == QL_OS.WINDOWS:
            # print("DeviceControl callback is at 0x%x" %self.loader.driver_object.MajorFunction[IRP_MJ_DEVICE_CONTROL])
            if self.ql.loader.driver_object.MajorFunction[IRP_MJ_DEVICE_CONTROL] == 0:
                # raise error?
                return (None, None, None)

            # create new memory region to store input data
            _ioctl_code, output_buffer_size, in_buffer = params
            # extract data transfer method
            devicetype, function, ctl_method, access = _ioctl_code

            input_buffer_size = len(in_buffer)
            input_buffer_addr = heap.alloc(input_buffer_size)
            alloc_addr.append(input_buffer_addr)
            self.ql.mem.write(input_buffer_addr, bytes(in_buffer))

            # create new memory region to store out data
            output_buffer_addr = heap.alloc(output_buffer_size)
            alloc_addr.append(output_buffer_addr)

            # allocate memory regions for IRP and IO_STACK_LOCATION
            if self.ql.archtype == QL_ARCH.X8664:
                irp_addr = heap.alloc(ctypes.sizeof(IRP64))
                alloc_addr.append(irp_addr)
                irpstack_addr = heap.alloc(ctypes.sizeof(IO_STACK_LOCATION64))
                alloc_addr.append(irpstack_addr)
                # setup irp stack parameters
                irpstack = IO_STACK_LOCATION64()
                # setup IRP structure
                irp = IRP64()
                irp.irpstack = ctypes.cast(irpstack_addr, ctypes.POINTER(IO_STACK_LOCATION64))
            else:
                irp_addr = heap.alloc(ctypes.sizeof(IRP32))
                alloc_addr.append(irp_addr)
                irpstack_addr = heap.alloc(ctypes.sizeof(IO_STACK_LOCATION32))
                alloc_addr.append(irpstack_addr)
                # setup irp stack parameters
                irpstack = IO_STACK_LOCATION32()
                # setup IRP structure
                irp = IRP32()
                irp.irpstack = ctypes.cast(irpstack_addr, ctypes.POINTER(IO_STACK_LOCATION32))

                #print("32 stack location size = 0x%x" %ctypes.sizeof(IO_STACK_LOCATION32))
                #print("32 status block size = 0x%x" %ctypes.sizeof(IO_STATUS_BLOCK32))
                #print("32 irp size = 0x%x" %ctypes.sizeof(IRP32))
                #print("32 IoStatus offset = 0x%x" %IRP32.IoStatus.offset)
                #print("32 UserIosb offset = 0x%x" %IRP32.UserIosb.offset)
                #print("32 UserEvent offset = 0x%x" %IRP32.UserEvent.offset)
                #print("32 UserBuffer offset = 0x%x" %IRP32.UserBuffer.offset)
                #print("32 irpstack offset = 0x%x" %IRP32.irpstack.offset)
                #print("irp at %x, irpstack at %x" %(irp_addr, irpstack_addr))

            self.ql.log.info("IRP is at 0x%x, IO_STACK_LOCATION is at 0x%x" %(irp_addr, irpstack_addr))

            irpstack.Parameters.DeviceIoControl.IoControlCode = ioctl_code(devicetype, function, ctl_method, access)
            irpstack.Parameters.DeviceIoControl.OutputBufferLength = output_buffer_size
            irpstack.Parameters.DeviceIoControl.InputBufferLength = input_buffer_size
            irpstack.Parameters.DeviceIoControl.Type3InputBuffer.value = input_buffer_addr # used by IOCTL_METHOD_NEITHER
            self.ql.mem.write(irpstack_addr, bytes(irpstack))

            if ctl_method == METHOD_NEITHER:
                irp.UserBuffer.value = output_buffer_addr  # used by IOCTL_METHOD_NEITHER

            # allocate memory for AssociatedIrp.SystemBuffer
            # used by IOCTL_METHOD_IN_DIRECT, IOCTL_METHOD_OUT_DIRECT and IOCTL_METHOD_BUFFERED
            system_buffer_size = max(input_buffer_size, output_buffer_size)
            system_buffer_addr = heap.alloc(system_buffer_size)
            alloc_addr.append(system_buffer_addr)

            # init data from input buffer
            self.ql.mem.write(system_buffer_addr, bytes(in_buffer))
            irp.AssociatedIrp.SystemBuffer.value = system_buffer_addr

            if ctl_method in (METHOD_IN_DIRECT, METHOD_OUT_DIRECT):
                # Create MDL structure for output data
                # used by both IOCTL_METHOD_IN_DIRECT and IOCTL_METHOD_OUT_DIRECT
                mdl = build_mdl(output_buffer_size)
                if self.ql.archtype == QL_ARCH.X8664:
                    mdl_addr = heap.alloc(ctypes.sizeof(MDL64))
                else:
                    mdl_addr = heap.alloc(ctypes.sizeof(MDL32))

                alloc_addr.append(mdl_addr)

                self.ql.mem.write(mdl_addr, bytes(mdl))
                irp.MdlAddress.value = mdl_addr

            # everything is done! Write IRP to memory
            self.ql.mem.write(irp_addr, bytes(irp))

            # set function args
            self.ql.log.info("Executing IOCTL with DeviceObject = 0x%x, IRP = 0x%x" %(self.ql.loader.driver_object.DeviceObject, irp_addr))
            # TODO: make sure this is indeed STDCALL
            self.ql.os.fcall = self.ql.os.fcall_select(STDCALL)
            self.ql.os.fcall.writeParams(((POINTER, self.ql.loader.driver_object.DeviceObject), (POINTER, irp_addr)))

            try:
                # now emulate IOCTL's DeviceControl
                self.ql.run(self.ql.loader.driver_object.MajorFunction[IRP_MJ_DEVICE_CONTROL])
            except UcError as err:
                verify_ret(self.ql, err)

            # read current IRP state
            if self.ql.archtype == QL_ARCH.X8664:
                irp_buffer = self.ql.mem.read(irp_addr, ctypes.sizeof(IRP64))
                irp = IRP64.from_buffer(irp_buffer)
            else:
                irp_buffer = self.ql.mem.read(irp_addr, ctypes.sizeof(IRP32))
                irp = IRP32.from_buffer(irp_buffer)

            io_status = irp.IoStatus

            # read output data
            output_data = b''
            if io_status.Status.Status >= 0:
                if ctl_method == METHOD_BUFFERED:
                    output_data = self.ql.mem.read(system_buffer_addr, io_status.Information.value)
                if ctl_method in (METHOD_IN_DIRECT, METHOD_OUT_DIRECT):
                    output_data = self.ql.mem.read(mdl.MappedSystemVa.value, io_status.Information.value)
                if ctl_method == METHOD_NEITHER:
                    output_data = self.ql.mem.read(output_buffer_addr, io_status.Information.value)

            # now free all alloc memory
            for addr in alloc_addr:
                # print("freeing heap memory at 0x%x" %addr) # FIXME: the output is not deterministic??
                heap.free(addr)
            #print("\n")

            return io_status.Status.Status, io_status.Information.value, output_data
        else: # TODO: IOCTL for non-Windows.
            pass        

from .debug import debug
from .util import byteorder, to_int, SegmentRegs, MissingOpcodeError

from .ELF import ELF32, enums

import logging
logger = logging.getLogger(__name__)


def execute_opcode(self) -> None:
    """
    Attempts to execute the current opcode `op`. The calls to `_<mnemonic name>` check whether the opcode corresponds to
    a mnemonic. This basically checks whether the opcode is supported and executes it if so.
    :param self: passed implicitly
    :return: None
    """

    self.eip += 1  # points to next data

    if self.opcode == 0x90:  # nop
        logger.debug(self.fmt.format(self.eip - 1, self.opcode))
        return

    opcode = self.opcode
    _off = 1

    # handle prefixes
    if opcode == 0x0F:
        op, = self.mem.get_eip(self.eip, 1)  # 0xYY
        self.eip += 1

        opcode = (opcode << 8) + op  # opcode <- 0x0FYY
        self.opcode = op
        _off += 1

    logger.debug(self.fmt.format(self.eip - _off, opcode))

    # try to execute `opcode`
    try:
        for instruction in self.instr[opcode]:
            if instruction():
                # `opcode` executed
                return
    except KeyError:
        # `opcode` was not found
        ...

    # No valid implementation for `opcode` was found (`instruction()` was always `False`)
    # or no implementation was found at all. Try to interpret `opcode` as being two bytes long.
    op, = self.mem.get_eip(self.eip, 1)
    self.eip += 1

    opcode = (opcode << 8) + op
    self.opcode = op

    try:
        for instruction in self.instr[opcode]:
            if instruction():
                # `opcode` executed
                return
    except KeyError:
        raise MissingOpcodeError(f'Opcode 0x{opcode:02x} is not recognized yet (@0x{self.eip - _off - 1:02x})')

    raise NotImplementedError(f'No suitable implementation found for opcode 0x{opcode:02x} (@0x{self.eip - _off - 1:02x})')
    

def override(self, name: str):
    old_size = getattr(self, name)
    self.current_mode = not self.current_mode
    setattr(self, name, self.sizes[self.current_mode])
    logger.debug('%s override (%d -> %d)', name, old_size, getattr(self, name))


def run(self):
    """
    Implements the basic CPU instruction cycle (https://en.wikipedia.org/wiki/Instruction_cycle)
    :param self: passed implicitly
    :param offset: location of the first opcode
    :return: None
    """

    # opcode perfixes
    pref_segments = {
        0x2E: SegmentRegs.CS,
        0x36: SegmentRegs.SS,
        0x3E: SegmentRegs.DS,
        0x26: SegmentRegs.ES,
        0x64: SegmentRegs.FS,
        0x65: SegmentRegs.GS
    }
    pref_op_size_override = {0x66, 0x67}
    pref_lock = {0xf0}

    prefixes = set(pref_segments) | pref_op_size_override | pref_lock
    self.running = True

    while self.running and self.eip + 1 in self.mem.bounds:
        overrides = []
        self.opcode, = self.mem.get(self.eip, 1)

        while self.opcode in prefixes:
            overrides.append(self.opcode)
            self.eip += 1
            self.opcode, = self.mem.get(self.eip, 1)

        # apply overrides
        size_override_active = False
        for ov in overrides:
            if ov == 0x66:
                if not size_override_active:
                    self.current_mode = not self.current_mode
                    size_override_active = True
                old_operand_size = self.operand_size
                self.operand_size = self.sizes[self.current_mode]
                logger.debug(
                    'Operand size override: %d -> %d',
                    old_operand_size, self.operand_size
                )
            elif ov == 0x67:
                if not size_override_active:
                    self.current_mode = not self.current_mode
                    size_override_active = True
                old_address_size = self.address_size
                self.address_size = self.sizes[self.current_mode]
                logger.debug(
                    'Address size override: %d -> %d',
                    old_address_size, self.address_size
                )
            elif ov in pref_segments:
                self.mem.segment_override = pref_segments[ov]
                logger.debug('Segment override: %s', self.mem.segment_override)
            elif ov in pref_lock:
                ...  # do nothing; all operations are atomic anyway. Right?

        self.execute_opcode()

        # undo any overrides
        # TODO: looks ugly
        self.mem.segment_override = SegmentRegs.DS
        self.current_mode = self.default_mode
        self.operand_size = self.sizes[self.current_mode]
        self.address_size = self.sizes[self.current_mode]

    ebx = int.from_bytes(self.reg.get(3, 4), byteorder)
    return ebx


def execute_bytes(self, data: bytes, offset=0):
    self.mem.set(offset, data)

    self.eip = offset
    self.code_segment_end = self.eip + len(data) - 1
    self.mem.program_break = self.code_segment_end
    
    return self.run()


def execute_file(self, fname: str, offset=0):
    with open(fname, 'rb') as f:
        data = f.read()
        self.mem.set(offset, data)

    self.eip = offset
    self.code_segment_end = self.eip + len(data) - 1
    self.mem.program_break = self.code_segment_end
    
    return self.run()
    
    
def execute_elf(self, fname: str, args=tuple()):
    with ELF32(fname) as elf:
        if elf.hdr.e_type != enums.e_type.ET_EXEC:
            raise ValueError(f'ELF file {elf.fname!r} is not executable (type: {elf.hdr.e_type})')
            
        max_memsz = max(
            phdr.p_vaddr + phdr.p_memsz
            for phdr in elf.phdrs
            if phdr.p_type == enums.p_type.PT_LOAD
        )

        if self.mem.size < max_memsz * 2:
            self.mem.size_set(max_memsz * 2)
            self.stack_init()
        
        for phdr in elf.phdrs:
            if phdr.p_type not in (enums.p_type.PT_LOAD, enums.p_type.PT_GNU_EH_FRAME):
                continue
                
            logger.debug(f'LOAD {phdr.p_memsz:10,d} bytes at address 0x{phdr.p_vaddr:09_x}')
            elf.file.seek(phdr.p_offset)
            
            self.mem.set(phdr.p_vaddr, elf.file.read(phdr.p_filesz))            
            self.mem.set(phdr.p_vaddr + phdr.p_filesz, bytearray(phdr.p_memsz - phdr.p_filesz))

    self.eip = elf.hdr.e_entry
    self.code_segment_end = self.eip + max_memsz - 1
    self.mem.program_break = self.code_segment_end

    # INITIALIZE STACK LAYOUT (http://asm.sourceforge.net/articles/startup.html)
    # push program name:
    PROG_NAME = b'tes\0'
    ARG_COUNT = 0
    
    self.stack_push(PROG_NAME)
    name_addr = to_int(self.reg.get(4, self.stack_address_size))  # esp

    # push command-line arguments:
    self.stack_push((0).to_bytes(4, byteorder))  # NULL - end of stack
    # INIT AUX VECTOR
    self.stack_push((0).to_bytes(4, byteorder))  # NULL - no vector
    # END INIT AUX VECTOR
    
    self.stack_push((0).to_bytes(4, byteorder))  # NULL - end of environment
    # no envirenment here
    self.stack_push((0).to_bytes(4, byteorder))  # NULL - end of arguments
    # no arguments here
    self.stack_push((name_addr).to_bytes(4, byteorder))  # pointer to program name
    self.stack_push(ARG_COUNT.to_bytes(4, byteorder))
    
    logger.debug(f'EXEC at 0x{self.eip:09_x}')
    
    return self.run()

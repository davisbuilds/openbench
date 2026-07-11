#!/usr/bin/env node
const fs = require('fs');

const ELF_CLASS_32 = 1;
const ELF_CLASS_64 = 2;
const ELF_DATA_2_LSB = 1;
const SHT_SYMTAB = 2;
const STT_TYPE = value => value & 0xf;

function parseElfHeader(buffer) {
  if (buffer.readUInt32BE(0) !== 0x7F454C46) throw new Error('Not an ELF file');
  const elfClass = buffer[4];
  const isLittleEndian = buffer[5] === ELF_DATA_2_LSB;
  const readHalf = isLittleEndian ? buffer.readUInt16LE : buffer.readUInt16BE;
  const readWord = isLittleEndian ? buffer.readUInt32LE : buffer.readUInt32BE;
  const readAddr = elfClass === ELF_CLASS_64
    ? (isLittleEndian ? buffer.readBigUInt64LE : buffer.readBigUInt64BE)
    : readWord;
  return {
    class: elfClass,
    isLittleEndian,
    shoff: Number(readAddr.call(buffer, elfClass === ELF_CLASS_64 ? 40 : 32)),
    shentsize: readHalf.call(buffer, elfClass === ELF_CLASS_64 ? 58 : 46),
    shnum: readHalf.call(buffer, elfClass === ELF_CLASS_64 ? 60 : 48),
    shstrndx: readHalf.call(buffer, elfClass === ELF_CLASS_64 ? 62 : 50)
  };
}

function parseSectionHeaders(buffer, header) {
  const readWord = header.isLittleEndian ? buffer.readUInt32LE : buffer.readUInt32BE;
  const readXword = header.class === ELF_CLASS_64
    ? (header.isLittleEndian ? buffer.readBigUInt64LE : buffer.readBigUInt64BE)
    : readWord;
  const sections = [];
  for (let i = 0; i < header.shnum; i++) {
    const off = header.shoff + i * header.shentsize;
    sections.push({
      name: readWord.call(buffer, off),
      type: readWord.call(buffer, off + 4),
      flags: Number(readXword.call(buffer, off + 8)),
      addr: Number(readXword.call(buffer, off + (header.class === ELF_CLASS_64 ? 16 : 12))),
      offset: Number(readXword.call(buffer, off + (header.class === ELF_CLASS_64 ? 24 : 16))),
      size: Number(readXword.call(buffer, off + (header.class === ELF_CLASS_64 ? 32 : 20))),
      link: readWord.call(buffer, off + (header.class === ELF_CLASS_64 ? 40 : 24)),
      info: readWord.call(buffer, off + (header.class === ELF_CLASS_64 ? 44 : 28)),
      entsize: Number(readXword.call(buffer, off + (header.class === ELF_CLASS_64 ? 56 : 36)))
    });
  }
  return sections;
}

function getString(buf, offset) {
  let end = offset;
  while (end < buf.length && buf[end] !== 0) end++;
  return buf.slice(offset, end).toString();
}

function sectionData(buffer, section) {
  return buffer.slice(section.offset, section.offset + section.size);
}

function parseSymbol(data, offset, elfClass) {
  if (elfClass === ELF_CLASS_32) {
    return {name: data.readUInt32LE(offset), value: data.readUInt32LE(offset + 4), size: data.readUInt32LE(offset + 8), info: data[offset + 12]};
  }
  return {name: data.readUInt32LE(offset), info: data[offset + 4], value: Number(data.readBigUInt64LE(offset + 8)), size: Number(data.readBigUInt64LE(offset + 16))};
}

function runElf(path) {
  const file = fs.readFileSync(path);
  const header = parseElfHeader(file);
  const sections = parseSectionHeaders(file, header);
  const shstrtab = sectionData(file, sections[header.shstrndx]);
  const memory = {};
  const names = new Map(sections.map((s, i) => [i, getString(shstrtab, s.name)]));

  for (let i = 0; i < sections.length; i++) {
    const name = names.get(i);
    if (name !== '.text' && name !== '.data' && name !== '.rodata') continue;
    const data = sectionData(file, sections[i]);
    for (let off = 0; off + 4 <= data.length; off += 4) {
      memory[String(sections[i].addr + off)] = data.readUInt32LE(off);
    }
  }

  // Parse the symbol table too, matching the upstream reference's coverage even
  // though the current output is only memory words.
  for (const section of sections) {
    if (section.type !== SHT_SYMTAB || !section.entsize) continue;
    const strtab = sectionData(file, sections[section.link]);
    const syms = sectionData(file, section);
    for (let off = 0; off + section.entsize <= syms.length; off += section.entsize) {
      const sym = parseSymbol(syms, off, header.class);
      void getString(strtab, sym.name);
      void STT_TYPE(sym.info);
    }
  }
  console.log(JSON.stringify(memory));
}

if (require.main === module) {
  if (process.argv.length < 3) {
    console.error('Usage: node extract.js <elf_file>');
    process.exit(1);
  }
  runElf(process.argv[2]);
}

"""
SHOW ENGINE INNODB STATUS parser.

Parses the dense text output into structured sections. Each section
is stored both as raw text (for the LLM to read) and as parsed JSON
where possible (for programmatic access).

Key sections:
    - SEMAPHORES: mutex/rw-lock waits (contention signals)
    - TRANSACTIONS: active transaction details
    - LATEST DETECTED DEADLOCK: full deadlock info
    - BUFFER POOL AND MEMORY: buffer pool stats
    - ROW OPERATIONS: rows inserted/updated/deleted/read per second
    - LOG: redo log info
"""

import re
import json
import logging

logger = logging.getLogger(__name__)

# Section headers in INNODB STATUS output
SECTION_PATTERN = re.compile(r"^-+\n(.+?)\n-+$", re.MULTILINE)

# Known section names
KNOWN_SECTIONS = [
    "BACKGROUND THREAD",
    "SEMAPHORES",
    "LATEST DETECTED DEADLOCK",
    "TRANSACTIONS",
    "FILE I/O",
    "INSERT BUFFER AND ADAPTIVE HASH INDEX",
    "LOG",
    "BUFFER POOL AND MEMORY",
    "ROW OPERATIONS",
]


def parse_innodb_status(raw_text: str) -> list[dict]:
    """
    Parse SHOW ENGINE INNODB STATUS into structured sections.

    Returns list of dicts with keys:
        section_name, section_data, parsed_json
    """
    if not raw_text:
        return []

    sections = _split_sections(raw_text)
    results = []

    for name, data in sections.items():
        parsed = None

        if name == "SEMAPHORES":
            parsed = _parse_semaphores(data)
        elif name == "LATEST DETECTED DEADLOCK":
            parsed = _parse_deadlock(data)
        elif name == "ROW OPERATIONS":
            parsed = _parse_row_operations(data)
        elif name == "LOG":
            parsed = _parse_log(data)
        elif name == "BUFFER POOL AND MEMORY":
            parsed = _parse_buffer_pool(data)

        results.append({
            "section_name": name,
            "section_data": data[:4000],  # Truncate very long sections
            "parsed_json": json.dumps(parsed) if parsed else None,
        })

    return results


def _split_sections(text: str) -> dict[str, str]:
    """Split the raw output into named sections.

    InnoDB STATUS format:
        ---------
        SECTION NAME
        ---------
        content lines...
    """
    sections = {}
    lines = text.split("\n")
    current_name = None
    current_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]
        # Check for section header pattern: ---\nNAME\n---
        if (line.startswith("---")
                and i + 2 < len(lines)
                and not lines[i + 1].startswith("---")
                and lines[i + 2].startswith("---")):
            # Save previous section
            if current_name:
                sections[current_name] = "\n".join(current_lines).strip()

            current_name = lines[i + 1].strip()
            current_lines = []
            i += 3  # Skip past the header block
            continue

        if current_name:
            current_lines.append(line)
        i += 1

    # Save last section
    if current_name and current_lines:
        sections[current_name] = "\n".join(current_lines).strip()

    return sections


def _parse_semaphores(text: str) -> dict:
    """Extract mutex/rw-lock wait counts from SEMAPHORES section."""
    result = {}

    # OS WAIT ARRAY INFO: reservation count N
    m = re.search(r"reservation count\s+(\d+)", text)
    if m:
        result["reservation_count"] = int(m.group(1))

    # Mutex spin waits N, rounds N, OS waits N
    m = re.search(r"Mutex spin waits\s+(\d+),\s*rounds\s+(\d+),\s*OS waits\s+(\d+)", text)
    if m:
        result["mutex_spin_waits"] = int(m.group(1))
        result["mutex_spin_rounds"] = int(m.group(2))
        result["mutex_os_waits"] = int(m.group(3))

    # RW-shared spins N, rounds N, OS waits N
    m = re.search(r"RW-shared spins\s+(\d+),\s*rounds\s+(\d+),\s*OS waits\s+(\d+)", text)
    if m:
        result["rw_shared_spins"] = int(m.group(1))
        result["rw_shared_rounds"] = int(m.group(2))
        result["rw_shared_os_waits"] = int(m.group(3))

    # RW-excl spins N, rounds N, OS waits N
    m = re.search(r"RW-excl spins\s+(\d+),\s*rounds\s+(\d+),\s*OS waits\s+(\d+)", text)
    if m:
        result["rw_excl_spins"] = int(m.group(1))
        result["rw_excl_rounds"] = int(m.group(2))
        result["rw_excl_os_waits"] = int(m.group(3))

    return result


def _parse_deadlock(text: str) -> dict:
    """Extract basic deadlock info."""
    result = {"has_deadlock": bool(text.strip())}

    # Count transactions involved
    trx_matches = re.findall(r"TRANSACTION\s+(\d+)", text)
    result["transaction_count"] = len(trx_matches)

    # Extract waiting-for-lock table names
    tables = re.findall(r"table `([^`]+)`.`([^`]+)`", text)
    result["tables_involved"] = [f"{s}.{t}" for s, t in set(tables)]

    return result


def _parse_row_operations(text: str) -> dict:
    """Extract row operation rates."""
    result = {}

    m = re.search(r"(\d+) inserts/s, (\d+) updates/s, (\d+) deletes/s, (\d+) reads/s", text)
    if m:
        result["inserts_per_sec"] = int(m.group(1))
        result["updates_per_sec"] = int(m.group(2))
        result["deletes_per_sec"] = int(m.group(3))
        result["reads_per_sec"] = int(m.group(4))

    m = re.search(r"Number of rows inserted\s+(\d+),\s*updated\s+(\d+),\s*deleted\s+(\d+),\s*read\s+(\d+)", text)
    if m:
        result["total_inserts"] = int(m.group(1))
        result["total_updates"] = int(m.group(2))
        result["total_deletes"] = int(m.group(3))
        result["total_reads"] = int(m.group(4))

    return result


def _parse_log(text: str) -> dict:
    """Extract redo log info."""
    result = {}

    m = re.search(r"Log sequence number\s+(\d+)", text)
    if m:
        result["log_sequence_number"] = int(m.group(1))

    m = re.search(r"Log flushed up to\s+(\d+)", text)
    if m:
        result["log_flushed_up_to"] = int(m.group(1))

    m = re.search(r"Pages flushed up to\s+(\d+)", text)
    if m:
        result["pages_flushed_up_to"] = int(m.group(1))

    m = re.search(r"Last checkpoint at\s+(\d+)", text)
    if m:
        result["last_checkpoint_at"] = int(m.group(1))

    # Calculate checkpoint age if we have both values
    if "log_sequence_number" in result and "last_checkpoint_at" in result:
        result["checkpoint_age"] = result["log_sequence_number"] - result["last_checkpoint_at"]

    return result


def _parse_buffer_pool(text: str) -> dict:
    """Extract buffer pool summary from status text."""
    result = {}

    m = re.search(r"Buffer pool size\s+(\d+)", text)
    if m:
        result["buffer_pool_size_pages"] = int(m.group(1))

    m = re.search(r"Free buffers\s+(\d+)", text)
    if m:
        result["free_buffers"] = int(m.group(1))

    m = re.search(r"Database pages\s+(\d+)", text)
    if m:
        result["database_pages"] = int(m.group(1))

    m = re.search(r"Modified db pages\s+(\d+)", text)
    if m:
        result["modified_pages"] = int(m.group(1))

    m = re.search(r"Pending reads\s+(\d+)", text)
    if m:
        result["pending_reads"] = int(m.group(1))

    return result

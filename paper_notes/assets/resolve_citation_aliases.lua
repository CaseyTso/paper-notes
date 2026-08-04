-- resolve_citation_aliases.lua
--
-- Bundled pre-citeproc Pandoc filter: rewrites old citation keys
-- (aliases) to their current keys inside the Pandoc AST, so citation
-- output never uses unsafe textual regex rewriting.
--
-- The old-to-current mapping is read from the JSON file named by the
-- PAPER_NOTES_ALIASES environment variable (the CLI writes it as
-- .paper-notes/citation-aliases.json). When the variable is unset or
-- the file is missing, the filter is a no-op.
--
-- Citing an old and a new key together (e.g. [@oldKey; @newKey]) yields
-- exactly one current identity: citations are deduplicated by their
-- resolved key inside each Cite element.

local aliases = {}

local function load_aliases()
  local path = os.getenv("PAPER_NOTES_ALIASES")
  if not path then
    return
  end
  local file = io.open(path, "r")
  if not file then
    return
  end
  local text = file:read("*a")
  file:close()
  local ok, decoded = pcall(function()
    return pandoc.json.decode(text)
  end)
  if not ok or type(decoded) ~= "table" then
    return
  end
  for old_key, current_key in pairs(decoded) do
    if type(old_key) == "string" and type(current_key) == "string" then
      aliases[old_key] = current_key
    end
  end
end

load_aliases()

function Cite(cite)
  local seen = {}
  local rewritten = {}
  for _, citation in ipairs(cite.citations) do
    local resolved = aliases[citation.id] or citation.id
    if not seen[resolved] then
      seen[resolved] = true
      rewritten[#rewritten + 1] = pandoc.Citation(
        resolved,
        citation.mode,
        citation.prefix,
        citation.suffix,
        citation.note_num,
        citation.hash
      )
    end
  end
  return pandoc.Cite(cite.content, rewritten)
end

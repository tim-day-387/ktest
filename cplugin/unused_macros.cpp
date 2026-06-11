// SPDX-License-Identifier: Apache-2.0
//
// unused-macros: find macros that are defined but never expanded or tested
// anywhere in the project. Header guards are ignored.

#include "lint.h"

#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Lex/MacroInfo.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include <map>
#include <memory>
#include <mutex>
#include <string>

using namespace clang;

namespace {

struct MacroDef {
  std::string Name;
  std::string Filename;
  unsigned Line;
  size_t Uses;
  bool IsHeaderGuard;
};

class UnusedMacrosLint : public Lint {
public:
  llvm::StringRef name() const override { return "unused-macros"; }
  std::unique_ptr<TUHandler> makeTUHandler() override;
  void report() override;

  // Cross-TU state, merged under Mutex at each TU's EndOfMainFile.
  // Key: "filename:line:name". Same macro included in many TUs hashes to the
  // same key, so cross-TU use counts merge correctly.
  std::mutex Mutex;
  std::map<std::string, MacroDef> AllMacros;
};

std::string makeMacroKey(StringRef Filename, unsigned Line,
                         StringRef Name) {
  std::string Key;
  Key.reserve(Filename.size() + Name.size() + 16);
  Key.append(Filename.data(), Filename.size());
  Key.push_back(':');
  Key.append(std::to_string(Line));
  Key.push_back(':');
  Key.append(Name.data(), Name.size());
  return Key;
}

class MacroTracker : public PPCallbacks {
public:
  MacroTracker(const SourceManager &SM, UnusedMacrosLint &L) : SM(SM), L(L) {}

  void MacroDefined(const Token &MacroNameTok,
                    const MacroDirective *MD) override {
    if (!MD)
      return;
    const clang::MacroInfo *MI = MD->getMacroInfo();
    if (!MI || MI->isBuiltinMacro())
      return;
    SourceLocation Loc = MI->getDefinitionLoc();
    if (Loc.isInvalid() || SM.isInSystemHeader(Loc))
      return;
    FileID FID = SM.getFileID(Loc);
    if (FID.isInvalid() || !SM.getFileEntryRefForID(FID).has_value())
      return;
    StringRef Name = MacroNameTok.getIdentifierInfo()->getName();
    StringRef RawFilename = SM.getFilename(Loc);
    if (RawFilename.empty())
      return;
    std::string Filename = canonicalizePath(SM, FID, RawFilename);
    unsigned Line = SM.getSpellingLineNumber(Loc);
    std::string Key = makeMacroKey(Filename, Line, Name);
    auto &Entry = LocalDefs[Key];
    Entry.Name = Name.str();
    Entry.Filename = std::move(Filename);
    Entry.Line = Line;
    Entry.MI = MI;
  }

  void MacroUndefined(const Token &MacroNameTok, const MacroDefinition &MD,
                      const MacroDirective * /*Undef*/) override {
    // #undef-ing a macro counts as a use of the previous definition.
    recordUse(MacroNameTok, MD);
  }

  void MacroExpands(const Token &MacroNameTok, const MacroDefinition &MD,
                    SourceRange /*Range*/,
                    const MacroArgs * /*Args*/) override {
    recordUse(MacroNameTok, MD);
  }

  void Defined(const Token &MacroNameTok, const MacroDefinition &MD,
               SourceRange /*Range*/) override {
    recordUse(MacroNameTok, MD);
  }

  void Ifdef(SourceLocation /*Loc*/, const Token &MacroNameTok,
             const MacroDefinition &MD) override {
    recordUse(MacroNameTok, MD);
  }

  void Ifndef(SourceLocation /*Loc*/, const Token &MacroNameTok,
              const MacroDefinition &MD) override {
    recordUse(MacroNameTok, MD);
  }

  void Elifdef(SourceLocation /*Loc*/, const Token &MacroNameTok,
               const MacroDefinition &MD) override {
    recordUse(MacroNameTok, MD);
  }

  void Elifndef(SourceLocation /*Loc*/, const Token &MacroNameTok,
                const MacroDefinition &MD) override {
    recordUse(MacroNameTok, MD);
  }

  void EndOfMainFile() override {
    std::unique_lock<std::mutex> LockGuard(L.Mutex);
    for (auto &KV : LocalDefs) {
      // isUsedForHeaderGuard() is set during end-of-file lexing — after
      // MacroDefined fires — so re-check it here.
      bool Guard = KV.second.MI && KV.second.MI->isUsedForHeaderGuard();
      auto it = L.AllMacros.find(KV.first);
      if (it == L.AllMacros.end()) {
        L.AllMacros.emplace(KV.first,
                            MacroDef{KV.second.Name, KV.second.Filename,
                                     KV.second.Line, 0, Guard});
      } else if (Guard) {
        it->second.IsHeaderGuard = true;
      }
    }
    for (auto &KV : LocalUses) {
      auto it = L.AllMacros.find(KV.first);
      if (it != L.AllMacros.end()) {
        it->second.Uses += KV.second;
      }
    }
  }

private:
  void recordUse(const Token &MacroNameTok, const MacroDefinition &MD) {
    const clang::MacroInfo *MI = MD.getMacroInfo();
    if (!MI || MI->isBuiltinMacro())
      return;
    SourceLocation Loc = MI->getDefinitionLoc();
    if (Loc.isInvalid() || SM.isInSystemHeader(Loc))
      return;
    FileID FID = SM.getFileID(Loc);
    if (FID.isInvalid() || !SM.getFileEntryRefForID(FID).has_value())
      return;
    StringRef Name = MacroNameTok.getIdentifierInfo()->getName();
    StringRef RawFilename = SM.getFilename(Loc);
    if (RawFilename.empty())
      return;
    std::string Filename = canonicalizePath(SM, FID, RawFilename);
    unsigned Line = SM.getSpellingLineNumber(Loc);
    LocalUses[makeMacroKey(Filename, Line, Name)]++;
  }

  struct LocalDef {
    std::string Name;
    std::string Filename;
    unsigned Line;
    const clang::MacroInfo *MI;
  };

  const SourceManager &SM;
  UnusedMacrosLint &L;
  std::map<std::string, LocalDef> LocalDefs;
  std::map<std::string, size_t> LocalUses;
};

class UnusedMacrosTUHandler : public TUHandler {
public:
  UnusedMacrosTUHandler(UnusedMacrosLint &L) : L(L) {}

  void registerPPCallbacks(CompilerInstance &CI) override {
    CI.getPreprocessor().addPPCallbacks(
        std::make_unique<MacroTracker>(CI.getSourceManager(), L));
  }

private:
  UnusedMacrosLint &L;
};

std::unique_ptr<TUHandler> UnusedMacrosLint::makeTUHandler() {
  return std::make_unique<UnusedMacrosTUHandler>(*this);
}

void UnusedMacrosLint::report() {
  for (auto &KV : AllMacros) {
    MacroDef &M = KV.second;
    if (M.Uses > 0)
      continue;
    if (M.IsHeaderGuard)
      continue;
    if (!inLustreTree(M.Filename))
      continue;

    llvm::StringRef Rel = stripLustrePrefix(M.Filename);
    llvm::errs() << M.Name << " " << Rel << ":" << M.Line << " MACRO "
                 << fileKind(Rel) << "\n";
  }
}

} // namespace

std::unique_ptr<Lint> makeUnusedMacrosLint() {
  return std::make_unique<UnusedMacrosLint>();
}

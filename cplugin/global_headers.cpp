// SPDX-License-Identifier: Apache-2.0
//
// global-headers: find functions and macros that are declared in a shared
// header but only ever used from a single directory. Such declarations
// belong in a header local to that directory, e.g. a symbol declared in
// lnet/include/lnet/lib-lnet.h that is only used by lnet/lnet/ should move
// to a header under lnet/lnet/.
//
// A function's definition anchors it to the defining directory just like a
// use does (a prototype cannot reasonably move away from its definition),
// unless the definition itself lives in the header (static inline).

#include "lint.h"

#include "clang/AST/AST.h"
#include "clang/AST/ASTContext.h"
#include "clang/ASTMatchers/ASTMatchFinder.h"
#include "clang/ASTMatchers/ASTMatchers.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Lex/MacroInfo.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Path.h"
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <utility>
#include <vector>

using namespace clang;
using namespace clang::ast_matchers;

static llvm::cl::opt<bool> HeaderUsage(
    "header-usage",
    llvm::cl::desc("Also print, per header, what percentage of its "
                   "declarations is used by multiple directories and a "
                   "breakdown of single-directory declarations by user "
                   "(global-headers lint)"),
    llvm::cl::cat(llvm::cl::getGeneralCategory()));

namespace {

struct SymbolInfo {
  std::string Name;
  std::string Header; // canonical absolute path of the declaring header
  unsigned Line = 0;
  std::set<std::string> UseDirs; // canonical absolute dirs of every user
};

class GlobalHeadersLint : public Lint {
public:
  llvm::StringRef name() const override { return "global-headers"; }
  std::unique_ptr<TUHandler> makeTUHandler() override;
  void report() override;
  void reportHeaderUsage();

  // Cross-TU state, merged under Mutex by each TU. Functions are keyed by
  // USR, macros by "filename:line:name", so entries for the same symbol
  // seen from different TUs merge their use directories.
  std::mutex Mutex;
  std::map<std::string, SymbolInfo> Functions;
  std::map<std::string, SymbolInfo> Macros;
};

std::string fileOfLoc(const SourceManager &SM, SourceLocation Loc) {
  SourceLocation E = SM.getExpansionLoc(Loc);
  if (E.isInvalid())
    return {};
  FileID FID = SM.getFileID(E);
  StringRef Raw = SM.getFilename(E);
  if (FID.isInvalid() || Raw.empty())
    return {};
  return canonicalizePath(SM, FID, Raw);
}

std::string dirOfLoc(const SourceManager &SM, SourceLocation Loc) {
  return llvm::sys::path::parent_path(fileOfLoc(SM, Loc)).str();
}

bool isLustreHeader(llvm::StringRef File) {
  return !File.empty() && inLustreTree(File) && fileKind(File) == "HEADER";
}

std::string makeMacroKey(llvm::StringRef Filename, unsigned Line,
                         llvm::StringRef Name) {
  std::string Key;
  Key.reserve(Filename.size() + Name.size() + 16);
  Key.append(Filename.data(), Filename.size());
  Key.push_back(':');
  Key.append(std::to_string(Line));
  Key.push_back(':');
  Key.append(Name.data(), Name.size());
  return Key;
}

class FnLocalityHandler : public MatchFinder::MatchCallback {
public:
  FnLocalityHandler(GlobalHeadersLint &L) : L(L) {}

  void run(const MatchFinder::MatchResult &Result) override {
    const SourceManager &SM = *Result.SourceManager;
    if (const auto *F = Result.Nodes.getNodeAs<FunctionDecl>("fnDecl")) {
      if (!F->hasBody())
        return; // Ignore '= delete' and '= default' definitions.

      if (auto *Templ = F->getInstantiatedFromMemberFunction())
        F = Templ;

      if (F->isTemplateInstantiation()) {
        F = F->getTemplateInstantiationPattern();
        assert(F);
      }

      if (SM.isInSystemHeader(F->getSourceRange().getBegin()))
        return;

      Defs.insert(F->getCanonicalDecl());
    } else if (const auto *R = Result.Nodes.getNodeAs<DeclRefExpr>("declRef")) {
      handleUse(R->getDecl(), R->getBeginLoc(), SM);
    } else if (const auto *R =
                   Result.Nodes.getNodeAs<MemberExpr>("memberRef")) {
      handleUse(R->getMemberDecl(), R->getBeginLoc(), SM);
    }
  }

  void finalize(const SourceManager &SM) {
    std::set<const FunctionDecl *> All = Defs;
    for (auto &KV : Uses)
      All.insert(KV.first);

    std::unique_lock<std::mutex> LockGuard(L.Mutex);

    for (const FunctionDecl *FD : All) {
      std::string HeaderFile;
      unsigned HeaderLine = 0;
      if (!findHeaderDecl(FD, SM, HeaderFile, HeaderLine))
        continue;

      std::string USR;
      if (!getUSRForDecl(FD, USR))
        continue;

      SymbolInfo &Info = L.Functions[USR];
      Info.Name = FD->getQualifiedNameAsString();
      Info.Header = std::move(HeaderFile);
      Info.Line = HeaderLine;

      if (const FunctionDecl *Def = FD->getDefinition()) {
        std::string DefFile = fileOfLoc(SM, Def->getSourceRange().getBegin());
        if (!DefFile.empty() && fileKind(DefFile) == "BODY")
          Info.UseDirs.insert(llvm::sys::path::parent_path(DefFile).str());
      }

      auto It = Uses.find(FD);
      if (It != Uses.end())
        Info.UseDirs.insert(It->second.begin(), It->second.end());
    }
  }

private:
  void handleUse(const ValueDecl *D, SourceLocation UseLoc,
                 const SourceManager &SM) {
    const auto *FD = dyn_cast<FunctionDecl>(D);
    if (!FD)
      return;

    if (SM.isInSystemHeader(FD->getSourceRange().getBegin()))
      return;
    if (FD->isTemplateInstantiation()) {
      FD = FD->getTemplateInstantiationPattern();
      assert(FD);
    }

    std::string Dir = dirOfLoc(SM, UseLoc);
    if (Dir.empty())
      return;

    Uses[FD->getCanonicalDecl()].insert(std::move(Dir));
  }

  // Find the redeclaration that lives in a Lustre-tree header; a static
  // inline defined in a header counts just like a prototype.
  bool findHeaderDecl(const FunctionDecl *FD, const SourceManager &SM,
                      std::string &File, unsigned &Line) {
    for (const FunctionDecl *R : FD->redecls()) {
      SourceLocation Loc = R->getSourceRange().getBegin();
      if (Loc.isInvalid() || SM.isInSystemHeader(Loc))
        continue;
      std::string F = fileOfLoc(SM, Loc);
      if (!isLustreHeader(F))
        continue;
      File = std::move(F);
      Line = SM.getSpellingLineNumber(SM.getExpansionLoc(Loc));
      return true;
    }
    return false;
  }

  GlobalHeadersLint &L;
  std::set<const FunctionDecl *> Defs;
  std::map<const FunctionDecl *, std::set<std::string>> Uses;
};

class MacroLocalityTracker : public PPCallbacks {
public:
  MacroLocalityTracker(const SourceManager &SM, GlobalHeadersLint &L)
      : SM(SM), L(L) {}

  void MacroDefined(const Token &MacroNameTok,
                    const MacroDirective *MD) override {
    if (!MD)
      return;
    std::string File;
    unsigned Line = 0;
    if (!headerDefLoc(MD->getMacroInfo(), File, Line))
      return;
    StringRef Name = MacroNameTok.getIdentifierInfo()->getName();
    auto &Def = LocalDefs[makeMacroKey(File, Line, Name)];
    Def.Name = Name.str();
    Def.Header = std::move(File);
    Def.Line = Line;
    Def.MI = MD->getMacroInfo();
  }

  void MacroUndefined(const Token &MacroNameTok, const MacroDefinition &MD,
                      const MacroDirective * /*Undef*/) override {
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
    // isUsedForHeaderGuard() is set during end-of-file lexing, so header
    // guards can only be filtered out here, not in MacroDefined().
    std::set<std::string> Guards;
    for (auto &KV : LocalDefs)
      if (KV.second.MI && KV.second.MI->isUsedForHeaderGuard())
        Guards.insert(KV.first);

    std::unique_lock<std::mutex> LockGuard(L.Mutex);
    for (auto &KV : LocalDefs) {
      if (Guards.count(KV.first))
        continue;
      SymbolInfo &Info = L.Macros[KV.first];
      Info.Name = KV.second.Name;
      Info.Header = KV.second.Header;
      Info.Line = KV.second.Line;
    }
    for (auto &KV : LocalUses) {
      if (Guards.count(KV.first))
        continue;
      SymbolInfo &Info = L.Macros[KV.first];
      Info.UseDirs.insert(KV.second.begin(), KV.second.end());
    }
  }

private:
  struct LocalDef {
    std::string Name;
    std::string Header;
    unsigned Line;
    const MacroInfo *MI = nullptr;
  };

  bool headerDefLoc(const MacroInfo *MI, std::string &File, unsigned &Line) {
    if (!MI || MI->isBuiltinMacro())
      return false;
    SourceLocation Loc = MI->getDefinitionLoc();
    if (Loc.isInvalid() || SM.isInSystemHeader(Loc))
      return false;
    FileID FID = SM.getFileID(Loc);
    if (FID.isInvalid() || !SM.getFileEntryRefForID(FID).has_value())
      return false;
    StringRef Raw = SM.getFilename(Loc);
    if (Raw.empty())
      return false;
    std::string F = canonicalizePath(SM, FID, Raw);
    if (!isLustreHeader(F))
      return false;
    File = std::move(F);
    Line = SM.getSpellingLineNumber(Loc);
    return true;
  }

  void recordUse(const Token &MacroNameTok, const MacroDefinition &MD) {
    std::string File;
    unsigned Line = 0;
    if (!headerDefLoc(MD.getMacroInfo(), File, Line))
      return;
    std::string Dir = dirOfLoc(SM, MacroNameTok.getLocation());
    if (Dir.empty())
      return;
    StringRef Name = MacroNameTok.getIdentifierInfo()->getName();
    LocalUses[makeMacroKey(File, Line, Name)].insert(std::move(Dir));
  }

  const SourceManager &SM;
  GlobalHeadersLint &L;
  std::map<std::string, LocalDef> LocalDefs;
  std::map<std::string, std::set<std::string>> LocalUses;
};

class GlobalHeadersTUHandler : public TUHandler {
public:
  GlobalHeadersTUHandler(GlobalHeadersLint &L) : L(L), Handler(L) {}

  void registerPPCallbacks(CompilerInstance &CI) override {
    CI.getPreprocessor().addPPCallbacks(
        std::make_unique<MacroLocalityTracker>(CI.getSourceManager(), L));
  }

  void registerMatchers(MatchFinder &Finder) override {
    Finder.addMatcher(
        functionDecl(isDefinition(), unless(isImplicit())).bind("fnDecl"),
        &Handler);
    Finder.addMatcher(declRefExpr().bind("declRef"), &Handler);
    Finder.addMatcher(memberExpr().bind("memberRef"), &Handler);
  }

  void finalize(ASTContext &Ctx) override {
    Handler.finalize(Ctx.getSourceManager());
  }

private:
  GlobalHeadersLint &L;
  FnLocalityHandler Handler;
};

std::unique_ptr<TUHandler> GlobalHeadersLint::makeTUHandler() {
  return std::make_unique<GlobalHeadersTUHandler>(*this);
}

void GlobalHeadersLint::report() {
  auto reportOne = [](const SymbolInfo &S, const char *Kind) {
    if (S.UseDirs.size() != 1)
      return;
    const std::string &Only = *S.UseDirs.begin();
    if (Only == llvm::sys::path::parent_path(S.Header))
      return; // already local to its users
    llvm::StringRef RelHeader = stripLustrePrefix(S.Header);
    if (RelHeader.contains("uapi/"))
      return; // UAPI headers cannot move into a kernel directory

    llvm::errs() << S.Name << " " << RelHeader << ":" << S.Line << " " << Kind
                 << " only-used-by " << stripLustrePrefix(Only) << "\n";
  };

  for (auto &KV : Functions)
    reportOne(KV.second, "FUNCTION");
  for (auto &KV : Macros)
    reportOne(KV.second, "MACRO");

  if (HeaderUsage) {
    llvm::errs() << "\n";
    reportHeaderUsage();
  }
}

// Secondary view: for every Lustre header, what fraction of its
// declarations (function prototypes, static inlines and macros) is shared
// by multiple directories, followed by a per-directory breakdown of the
// declarations used by only that directory. A function's defining
// directory counts as a use, matching the locality rule above. Symbols
// with no uses at all still count toward the total.
void GlobalHeadersLint::reportHeaderUsage() {
  struct HeaderStats {
    unsigned Total = 0;
    unsigned Multi = 0;
    unsigned Unused = 0;
    std::set<std::string> MultiDirs;
    std::map<std::string, unsigned> SingleByDir;
  };
  std::map<std::string, HeaderStats> Headers;

  auto addSymbols = [&](const std::map<std::string, SymbolInfo> &Syms) {
    for (const auto &KV : Syms) {
      const SymbolInfo &S = KV.second;
      HeaderStats &H = Headers[S.Header];
      H.Total++;
      if (S.UseDirs.size() > 1) {
        H.Multi++;
        H.MultiDirs.insert(S.UseDirs.begin(), S.UseDirs.end());
      } else if (S.UseDirs.size() == 1)
        H.SingleByDir[*S.UseDirs.begin()]++;
      else
        H.Unused++;
    }
  };
  addSymbols(Functions);
  addSymbols(Macros);

  for (const auto &KV : Headers) {
    const HeaderStats &H = KV.second;
    llvm::errs() << "HEADER " << stripLustrePrefix(KV.first)
                 << " declarations=" << H.Total << "\n";

    auto printLine = [&](llvm::StringRef Label, unsigned Count) {
      llvm::errs() << "  " << Label << " " << (100 * Count / H.Total) << "% ("
                   << Count << "/" << H.Total << ")\n";
    };

    if (H.Multi) {
      printLine("multiple-dirs", H.Multi);
      for (const std::string &Dir : H.MultiDirs)
        llvm::errs() << "    " << stripLustrePrefix(Dir) << "\n";
    }

    std::vector<std::pair<std::string, unsigned>> Dirs(H.SingleByDir.begin(),
                                                       H.SingleByDir.end());
    llvm::sort(Dirs, [](const auto &A, const auto &B) {
      return A.second != B.second ? A.second > B.second : A.first < B.first;
    });
    for (const auto &D : Dirs)
      printLine(("only " + stripLustrePrefix(D.first)).str(), D.second);

    if (H.Unused)
      printLine("unused", H.Unused);
  }
}

} // namespace

std::unique_ptr<Lint> makeGlobalHeadersLint() {
  return std::make_unique<GlobalHeadersLint>();
}

// SPDX-License-Identifier: Apache-2.0
//
// xunused - find unused C/C++ functions across a project.
// Upstream: https://github.com/mgehre/xunused
//
// Modified for Lustre

#include "clang/AST/AST.h"
#include "clang/AST/ASTContext.h"
#include "clang/ASTMatchers/ASTMatchFinder.h"
#include "clang/ASTMatchers/ASTMatchers.h"
#include "clang/Basic/Version.h"
// clang 22 moved the option tables out of clangDriver into a new clangOptions
// library, relocating this header from clang/Driver/ to clang/Options/.
#if CLANG_VERSION_MAJOR >= 22
#include "clang/Options/Options.h"
#else
#include "clang/Driver/Options.h"
#endif
#include "clang/Frontend/ASTConsumers.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendActions.h"
#include "clang/Index/USRGeneration.h"
#include "clang/Lex/MacroInfo.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "clang/Tooling/AllTUsExecution.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Signals.h"
#include <memory>
#include <mutex>
#include <map>
#include <fcntl.h>
#include <unistd.h>


using namespace clang;
using namespace clang::ast_matchers;

template <class T, class Comp, class Alloc, class Predicate>
void discard_if(std::set<T, Comp, Alloc> &c, Predicate pred) {
  for (auto it{c.begin()}, end{c.end()}; it != end;) {
    if (pred(*it)) {
      it = c.erase(it);
    } else {
      ++it;
    }
  }
}

struct DeclLoc {
  DeclLoc() = default;
  DeclLoc(std::string Filename, unsigned Line)
      : Filename(std::move(Filename)), Line(Line) {}
  SmallString<128> Filename;
  unsigned Line;
};

struct DefInfo {
  const FunctionDecl *Definition;
  size_t Uses;
  std::string Name;
  std::string Filename;
  unsigned Line;
  std::vector<DeclLoc> Declarations;
};

std::mutex Mutex;
std::map<std::string, DefInfo> AllDecls;

struct MacroDef {
  std::string Name;
  std::string Filename;
  unsigned Line;
  size_t Uses;
  bool IsHeaderGuard;
};

std::mutex MacroMutex;
// Key: "filename:line:name". Same macro included in many TUs hashes to the
// same key, so cross-TU use counts merge correctly.
std::map<std::string, MacroDef> AllMacros;

static std::string makeMacroKey(StringRef Filename, unsigned Line,
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

bool getUSRForDecl(const Decl *Decl, std::string &USR) {
  llvm::SmallVector<char, 128> Buff;

  if (index::generateUSRForDecl(Decl, Buff))
    return false;

  USR = std::string(Buff.data(), Buff.size());
  return true;
}

/// Returns all declarations that are not the definition of F
std::vector<DeclLoc> getDeclarations(const FunctionDecl *F,
                                     const SourceManager &SM) {
  std::vector<DeclLoc> Decls;
  for (const FunctionDecl *R : F->redecls()) {
    if (R->doesThisDeclarationHaveABody())
      continue;
    auto Begin = R->getSourceRange().getBegin();
    Decls.emplace_back(SM.getFilename(Begin).str(), SM.getSpellingLineNumber(Begin));
    SM.getFileManager().makeAbsolutePath(Decls.back().Filename);
  }
  return Decls;
}

class FunctionDeclMatchHandler : public MatchFinder::MatchCallback {
public:
  void finalize(const SourceManager &SM) {
    std::unique_lock<std::mutex> LockGuard(Mutex);

    // Record definition info for every function defined in this TU, not just
    // the unused ones, so the cross-TU Uses tally has a meaningful Definition
    // to attach to.
    for (auto *F : Defs) {
      F = F->getDefinition();
      if (!F)
        continue;
      std::string USR;
      if (!getUSRForDecl(F, USR))
        continue;
      auto it_inserted = AllDecls.emplace(std::move(USR), DefInfo{F, 0});
      if (!it_inserted.second) {
        it_inserted.first->second.Definition = F;
      }
      it_inserted.first->second.Name = F->getQualifiedNameAsString();

      auto Begin = F->getSourceRange().getBegin();
      it_inserted.first->second.Filename = SM.getFilename(Begin);
      it_inserted.first->second.Line = SM.getSpellingLineNumber(Begin);

      it_inserted.first->second.Declarations = getDeclarations(F, SM);
    }

    // Weak functions are not the definitive definition. Remove it from
    // Defs before checking which uses we need to consider in other TUs,
    // so the functions overwritting the weak definition here are marked
    // as used.
    discard_if(Defs, [](const FunctionDecl *FD) { return FD->isWeak(); });

    std::vector<const FunctionDecl *> ExternalUses;

    std::set_difference(Uses.begin(), Uses.end(), Defs.begin(), Defs.end(),
                        std::back_inserter(ExternalUses));

    for (auto *F : Uses) {
      // llvm::errs() << "ExternalUses: " << F->getNameAsString() << "\n";
      std::string USR;
      if (!getUSRForDecl(F, USR))
        continue;
      // llvm::errs() << "ExternalUses: " << USR << "\n";
      auto it_inserted = AllDecls.emplace(std::move(USR), DefInfo{nullptr, 1});
      if (!it_inserted.second) {
        it_inserted.first->second.Uses++;
      }
    }
  }

  void handleUse(const ValueDecl *D, const SourceManager *SM) {
    auto *FD = dyn_cast<FunctionDecl>(D);
    if (!FD)
      return;

    if (SM->isInSystemHeader(FD->getSourceRange().getBegin()))
      return;
    if (FD->isTemplateInstantiation()) {
      FD = FD->getTemplateInstantiationPattern();
      assert(FD);
    }

#if 0
    llvm::errs() << "Use ";
    FD->printName(llvm::errs());
    //llvm::errs() << " USR:" << USR;
    llvm::errs() << "\n";
#endif
    Uses.insert(FD->getCanonicalDecl());
  }
  void run(const MatchFinder::MatchResult &Result) override {
    if (const auto *F = Result.Nodes.getNodeAs<FunctionDecl>("fnDecl")) {
      if (!F->hasBody())
        return; // Ignore '= delete' and '= default' definitions.

      if (auto *Templ = F->getInstantiatedFromMemberFunction())
        F = Templ;

      if (F->isTemplateInstantiation()) {
        F = F->getTemplateInstantiationPattern();
        assert(F);
      }

      auto Begin = F->getSourceRange().getBegin();
      if (Result.SourceManager->isInSystemHeader(Begin))
        return;

      auto *MD = dyn_cast<CXXMethodDecl>(F);
      if (MD) {
        if (MD->isVirtual()
        #if CLANG_VERSION_MAJOR >= 18
          && !MD->isPureVirtual()
        #else
          && !MD->isPure()
        #endif
          && MD->size_overridden_methods())
          return; // overriding method
        if (isa<CXXDestructorDecl>(MD))
          return; // We don't see uses of destructors.
      }

      if (F->isMain())
        return;
#if 0
      llvm::errs() << "FunctionDecl ";
      F->printName(llvm::errs());
      llvm::errs() << " USR:" << USR << "\n";
#endif
      Defs.insert(F->getCanonicalDecl());

      // __attribute__((constructor())) are always used
      if (F->hasAttr<ConstructorAttr>())
        handleUse(F, Result.SourceManager);

    } else if (const auto *R = Result.Nodes.getNodeAs<DeclRefExpr>("declRef")) {
      handleUse(R->getDecl(), Result.SourceManager);
    } else if (const auto *R =
                   Result.Nodes.getNodeAs<MemberExpr>("memberRef")) {
      handleUse(R->getMemberDecl(), Result.SourceManager);
    } else if (const auto *R = Result.Nodes.getNodeAs<CXXConstructExpr>(
                   "cxxConstructExpr")) {
      handleUse(R->getConstructor(), Result.SourceManager);
    }
  }

  std::set<const FunctionDecl *> Defs;
  std::set<const FunctionDecl *> Uses;
};

// Canonicalize a path for use as a cross-TU map key. The same header may be
// reached via different relative paths in different TUs (e.g.
// "include/uapi/../../ldlm/foo.h" vs "ldlm/foo.h"); without normalization
// those produce distinct keys and use-counts never merge.
static std::string canonicalizePath(const SourceManager &SM, FileID FID,
                                    StringRef Fallback) {
  if (auto FE = SM.getFileEntryRefForID(FID)) {
    StringRef Real = FE->getFileEntry().tryGetRealPathName();
    if (!Real.empty())
      return Real.str();
  }
  llvm::SmallString<256> Buf(Fallback);
  SM.getFileManager().makeAbsolutePath(Buf);
  llvm::sys::path::remove_dots(Buf, /*remove_dot_dot=*/true);
  return std::string(Buf);
}

class MacroTracker : public PPCallbacks {
public:
  MacroTracker(const SourceManager &SM) : SM(SM) {}

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
    std::unique_lock<std::mutex> LockGuard(MacroMutex);
    for (auto &KV : LocalDefs) {
      // isUsedForHeaderGuard() is set during end-of-file lexing — after
      // MacroDefined fires — so re-check it here.
      bool Guard = KV.second.MI && KV.second.MI->isUsedForHeaderGuard();
      auto it = AllMacros.find(KV.first);
      if (it == AllMacros.end()) {
        AllMacros.emplace(KV.first,
                          MacroDef{KV.second.Name, KV.second.Filename,
                                   KV.second.Line, 0, Guard});
      } else if (Guard) {
        it->second.IsHeaderGuard = true;
      }
    }
    for (auto &KV : LocalUses) {
      auto it = AllMacros.find(KV.first);
      if (it != AllMacros.end()) {
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
  std::map<std::string, LocalDef> LocalDefs;
  std::map<std::string, size_t> LocalUses;
};

class XUnusedASTConsumer : public ASTConsumer {
public:
  XUnusedASTConsumer() {
    Matcher.addMatcher(
        functionDecl(isDefinition(), unless(isImplicit())).bind("fnDecl"),
        &Handler);
    Matcher.addMatcher(declRefExpr().bind("declRef"), &Handler);
    Matcher.addMatcher(memberExpr().bind("memberRef"), &Handler);
    Matcher.addMatcher(cxxConstructExpr().bind("cxxConstructExpr"), &Handler);
  }

  void HandleTranslationUnit(ASTContext &Context) override {
    Matcher.matchAST(Context);
    Handler.finalize(Context.getSourceManager());
  }

private:
  FunctionDeclMatchHandler Handler;
  MatchFinder Matcher;
};

// For each source file provided to the tool, a new FrontendAction is created.
class XUnusedFrontendAction : public ASTFrontendAction {
public:
  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &CI,
                                                 StringRef /*File*/) override {
    CI.getPreprocessor().addPPCallbacks(
        std::make_unique<MacroTracker>(CI.getSourceManager()));
    return std::make_unique<XUnusedASTConsumer>();
  }
};

class XUnusedFrontendActionFactory : public tooling::FrontendActionFactory {
public:
  std::unique_ptr<FrontendAction> create() override { return std::make_unique<XUnusedFrontendAction>(); }
};

int main(int argc, const char **argv) {
  llvm::sys::PrintStackTraceOnErrorSignal(argv[0]);

  const char *Overview = R"(
  xunused is tool to find unused functions and methods across a whole C/C++ project.
  )";

  tooling::ExecutorName.setInitialValue("all-TUs");
#if 1
  auto Executor = clang::tooling::createExecutorFromCommandLineArgs(
      argc, argv, llvm::cl::getGeneralCategory(), Overview);
  if (!Executor) {
    llvm::errs() << llvm::toString(Executor.takeError()) << "\n";
    return 1;
  }
  // Silence the "[N/M] Processing file ..." progress lines (and any other
  // clang diagnostics) emitted to stderr during execute() by redirecting
  // fd 2 to /dev/null for the duration of the run.
  fflush(stderr);
  int saved_stderr = dup(STDERR_FILENO);
  int devnull = open("/dev/null", O_WRONLY);
  if (devnull >= 0) {
    dup2(devnull, STDERR_FILENO);
    close(devnull);
  }

  auto Err =
      Executor->get()->execute(std::unique_ptr<XUnusedFrontendActionFactory>(
          new XUnusedFrontendActionFactory()));

  fflush(stderr);
  if (saved_stderr >= 0) {
    dup2(saved_stderr, STDERR_FILENO);
    close(saved_stderr);
  }
#else
  static llvm::cl::OptionCategory XUnusedCategory("xunused options");
  CommonOptionsParser op(argc, argv, XUnusedCategory);
  AllTUsToolExecutor > Executor(op.getCompilations(), /*ThreadCount=*/0);
  auto Err = Executor.execute(std::unique_ptr<XUnusedFrontendActionFactory>(
      new XUnusedFrontendActionFactory()));
#endif

  if (Err) {
    llvm::errs() << llvm::toString(std::move(Err)) << "\n";
  }

  auto endsWith = [](StringRef S, StringRef Suffix) {
    return S.size() >= Suffix.size() &&
           S.substr(S.size() - Suffix.size()) == Suffix;
  };

  auto stripLustrePrefix = [](StringRef Path) {
    size_t Pos = Path.find("lustre-release/");
    if (Pos != StringRef::npos)
      return Path.substr(Pos + sizeof("lustre-release/") - 1);
    return Path;
  };

  auto fileKind = [&](StringRef Rel) {
    return (endsWith(Rel, ".h") || endsWith(Rel, ".hpp") ||
            endsWith(Rel, ".hxx") || endsWith(Rel, ".hh") ||
            endsWith(Rel, ".H"))
               ? "HEADER"
               : "BODY";
  };

  for (auto &KV : AllDecls) {
    DefInfo &I = KV.second;
    // TODO @timday: hardcode path to ignore system headers
    if (!I.Definition)
      continue;
    if (I.Uses > 0)
      continue;
    if (I.Filename.find("lustre-release") == std::string::npos)
      continue;

    StringRef Rel = stripLustrePrefix(I.Filename);
    llvm::errs() << I.Name << " " << Rel << " " << fileKind(Rel) << "\n";
  }

  for (auto &KV : AllMacros) {
    MacroDef &M = KV.second;
    if (M.Uses > 0)
      continue;
    if (M.IsHeaderGuard)
      continue;
    if (M.Filename.find("lustre-release") == std::string::npos)
      continue;

    StringRef Rel = stripLustrePrefix(M.Filename);
    llvm::errs() << M.Name << " " << Rel << ":" << M.Line << " MACRO "
                 << fileKind(Rel) << "\n";
  }
}

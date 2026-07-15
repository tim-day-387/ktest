// SPDX-License-Identifier: Apache-2.0
//
// unused-functions: find functions and methods that are defined but never
// used anywhere in the project. Templates, virtual functions, constructors
// and functions with static linkage are all taken into account.
//
// Derived from xunused (https://github.com/mgehre/xunused), modified for
// Lustre.

#include "lint.h"

#include "clang/AST/AST.h"
#include "clang/AST/ASTContext.h"
#include "clang/ASTMatchers/ASTMatchFinder.h"
#include "clang/ASTMatchers/ASTMatchers.h"
#include "clang/Basic/Version.h"
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <vector>

using namespace clang;
using namespace clang::ast_matchers;

namespace {

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

class UnusedFunctionsLint : public Lint {
public:
  llvm::StringRef name() const override { return "unused-functions"; }
  std::unique_ptr<TUHandler> makeTUHandler() override;
  void report() override;

  // Cross-TU state, merged under Mutex by each TU's finalize().
  std::mutex Mutex;
  std::map<std::string, DefInfo> AllDecls;
};

/// Returns all declarations that are not the definition of F
std::vector<DeclLoc> getDeclarations(const FunctionDecl *F,
                                     const SourceManager &SM) {
  std::vector<DeclLoc> Decls;
  for (const FunctionDecl *R : F->redecls()) {
    if (R->doesThisDeclarationHaveABody())
      continue;
    auto Begin = R->getSourceRange().getBegin();
    Decls.emplace_back(canonicalizePath(SM, SM.getFileID(Begin),
                                        SM.getFilename(Begin)),
                       SM.getSpellingLineNumber(Begin));
  }
  return Decls;
}

class FunctionDeclMatchHandler : public MatchFinder::MatchCallback {
public:
  FunctionDeclMatchHandler(UnusedFunctionsLint &L) : L(L) {}

  void finalize(const SourceManager &SM) {
    std::unique_lock<std::mutex> LockGuard(L.Mutex);

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
      auto it_inserted = L.AllDecls.emplace(std::move(USR), DefInfo{F, 0});
      if (!it_inserted.second) {
        it_inserted.first->second.Definition = F;
      }
      it_inserted.first->second.Name = F->getQualifiedNameAsString();

      auto Begin = F->getSourceRange().getBegin();
      it_inserted.first->second.Filename =
          canonicalizePath(SM, SM.getFileID(Begin), SM.getFilename(Begin));
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
      std::string USR;
      if (!getUSRForDecl(F, USR))
        continue;
      auto it_inserted = L.AllDecls.emplace(std::move(USR), DefInfo{nullptr, 1});
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

      // Functions synthesized by macro expansion (module_param's __check_*,
      // DEFINE_GUARD cleanup helpers, ...) have no source file of their own
      // to report against.
      if (Begin.isMacroID())
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

private:
  UnusedFunctionsLint &L;
  std::set<const FunctionDecl *> Defs;
  std::set<const FunctionDecl *> Uses;
};

class UnusedFunctionsTUHandler : public TUHandler {
public:
  UnusedFunctionsTUHandler(UnusedFunctionsLint &L) : Handler(L) {}

  void registerMatchers(MatchFinder &Finder) override {
    Finder.addMatcher(
        functionDecl(isDefinition(), unless(isImplicit())).bind("fnDecl"),
        &Handler);
    Finder.addMatcher(declRefExpr().bind("declRef"), &Handler);
    Finder.addMatcher(memberExpr().bind("memberRef"), &Handler);
    Finder.addMatcher(cxxConstructExpr().bind("cxxConstructExpr"), &Handler);
  }

  void finalize(ASTContext &Ctx) override {
    Handler.finalize(Ctx.getSourceManager());
  }

private:
  FunctionDeclMatchHandler Handler;
};

std::unique_ptr<TUHandler> UnusedFunctionsLint::makeTUHandler() {
  return std::make_unique<UnusedFunctionsTUHandler>(*this);
}

void UnusedFunctionsLint::report() {
  for (auto &KV : AllDecls) {
    DefInfo &I = KV.second;
    // TODO @timday: hardcode path to ignore system headers
    if (!I.Definition)
      continue;
    if (I.Uses > 0)
      continue;
    if (!inLustreTree(I.Filename))
      continue;

    llvm::StringRef Rel = stripLustrePrefix(I.Filename);
    llvm::errs() << I.Name << " " << Rel << " " << fileKind(Rel) << "\n";
  }
}

} // namespace

std::unique_ptr<Lint> makeUnusedFunctionsLint() {
  return std::make_unique<UnusedFunctionsLint>();
}

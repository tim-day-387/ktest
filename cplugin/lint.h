// SPDX-License-Identifier: Apache-2.0
//
// Lint framework for the ktest cc plugin.
//
// Each lint lives in its own .cpp file and implements two pieces:
//
//   - TUHandler: per-translation-unit hooks. A fresh handler is created
//     for every TU (TUs are processed in parallel), so per-TU state lives
//     here and finalize() merges it into the lint's cross-TU state under
//     the lint's own lock.
//
//   - Lint: the lint itself. Owns the cross-TU state and prints the
//     findings once every TU has been processed.

#ifndef CCPLUGIN_LINT_H
#define CCPLUGIN_LINT_H

#include "clang/Basic/SourceLocation.h"
#include "llvm/ADT/StringRef.h"
#include <memory>
#include <string>
#include <vector>

namespace clang {
class ASTContext;
class CompilerInstance;
class Decl;
class SourceManager;
namespace ast_matchers {
class MatchFinder;
}
} // namespace clang

class TUHandler {
public:
  virtual ~TUHandler() = default;
  // Called at TU creation time; attach PPCallbacks here.
  virtual void registerPPCallbacks(clang::CompilerInstance &CI) {}
  // Register AST matchers; the finder runs once over the whole TU.
  virtual void registerMatchers(clang::ast_matchers::MatchFinder &Finder) {}
  // Called after matching completes; merge per-TU results.
  virtual void finalize(clang::ASTContext &Ctx) {}
};

class Lint {
public:
  virtual ~Lint() = default;
  virtual llvm::StringRef name() const = 0;
  virtual std::unique_ptr<TUHandler> makeTUHandler() = 0;
  // Print findings (to stderr) after all TUs have been processed.
  virtual void report() = 0;
};

// All known lints, in report order.
std::vector<std::unique_ptr<Lint>> makeAllLints();

std::unique_ptr<Lint> makeUnusedFunctionsLint();
std::unique_ptr<Lint> makeUnusedMacrosLint();
std::unique_ptr<Lint> makeGlobalHeadersLint();
std::unique_ptr<Lint> makeFeatureMacrosLint();

// Shared report helpers.
bool inLustreTree(llvm::StringRef Path);
llvm::StringRef stripLustrePrefix(llvm::StringRef Path);
llvm::StringRef fileKind(llvm::StringRef Path); // "HEADER" or "BODY"

// Shared AST/source helpers.
bool getUSRForDecl(const clang::Decl *Decl, std::string &USR);
std::string canonicalizePath(const clang::SourceManager &SM, clang::FileID FID,
                             llvm::StringRef Fallback);

#endif // CCPLUGIN_LINT_H

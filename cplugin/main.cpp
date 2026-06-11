// SPDX-License-Identifier: Apache-2.0
//
// ktest cc plugin - run source-level lints across a whole C/C++ project.
// Derived from xunused (https://github.com/mgehre/xunused), modified for
// Lustre.
//
// Each lint lives in its own .cpp file (see lint.h). All lints run by
// default; use -lint/-no-lint to choose which run and -list-lints to see
// what is available.

#include "lint.h"

#include "clang/ASTMatchers/ASTMatchFinder.h"
#include "clang/Basic/Version.h"
// clang 22 moved the option tables out of clangDriver into a new clangOptions
// library, relocating this header from clang/Driver/ to clang/Options/.
#if CLANG_VERSION_MAJOR >= 22
#include "clang/Options/Options.h"
#else
#include "clang/Driver/Options.h"
#endif
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendActions.h"
#include "clang/Tooling/AllTUsExecution.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Signals.h"
#include <fcntl.h>
#include <memory>
#include <unistd.h>

using namespace clang;
using namespace clang::ast_matchers;

static llvm::cl::list<std::string> EnabledLintNames(
    "lint",
    llvm::cl::desc("Run only the named lints (repeatable or comma "
                   "separated); default is all lints"),
    llvm::cl::value_desc("name"), llvm::cl::CommaSeparated,
    llvm::cl::cat(llvm::cl::getGeneralCategory()));

static llvm::cl::list<std::string> DisabledLintNames(
    "no-lint",
    llvm::cl::desc("Do not run the named lints (repeatable or comma "
                   "separated)"),
    llvm::cl::value_desc("name"), llvm::cl::CommaSeparated,
    llvm::cl::cat(llvm::cl::getGeneralCategory()));

static llvm::cl::opt<bool>
    ListLints("list-lints", llvm::cl::desc("List available lints and exit"),
              llvm::cl::cat(llvm::cl::getGeneralCategory()));

namespace {

class LintASTConsumer : public ASTConsumer {
public:
  LintASTConsumer(std::vector<std::unique_ptr<TUHandler>> Handlers)
      : Handlers(std::move(Handlers)) {
    for (auto &H : this->Handlers)
      H->registerMatchers(Matcher);
  }

  void HandleTranslationUnit(ASTContext &Context) override {
    Matcher.matchAST(Context);
    for (auto &H : Handlers)
      H->finalize(Context);
  }

private:
  std::vector<std::unique_ptr<TUHandler>> Handlers;
  MatchFinder Matcher;
};

// For each source file provided to the tool, a new FrontendAction is created.
class LintFrontendAction : public ASTFrontendAction {
public:
  LintFrontendAction(const std::vector<Lint *> &Lints) : Lints(Lints) {}

  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &CI,
                                                 StringRef /*File*/) override {
    std::vector<std::unique_ptr<TUHandler>> Handlers;
    for (Lint *L : Lints) {
      auto Handler = L->makeTUHandler();
      Handler->registerPPCallbacks(CI);
      Handlers.push_back(std::move(Handler));
    }
    return std::make_unique<LintASTConsumer>(std::move(Handlers));
  }

private:
  const std::vector<Lint *> &Lints;
};

class LintFrontendActionFactory : public tooling::FrontendActionFactory {
public:
  LintFrontendActionFactory(const std::vector<Lint *> &Lints) : Lints(Lints) {}

  std::unique_ptr<FrontendAction> create() override {
    return std::make_unique<LintFrontendAction>(Lints);
  }

private:
  const std::vector<Lint *> &Lints;
};

} // namespace

int main(int argc, const char **argv) {
  llvm::sys::PrintStackTraceOnErrorSignal(argv[0]);

  const char *Overview = R"(
  ccplugin runs source-level lints across a whole C/C++ project. Use
  -list-lints to see the available lints and -lint/-no-lint to choose
  which ones run (default: all).
  )";

  auto AllLints = makeAllLints();

  tooling::ExecutorName.setInitialValue("all-TUs");
  auto Executor = clang::tooling::createExecutorFromCommandLineArgs(
      argc, argv, llvm::cl::getGeneralCategory(), Overview);

  // Options are parsed by now even if executor creation failed (e.g. no
  // compilation database given), so -list-lints works standalone.
  if (ListLints) {
    for (auto &L : AllLints)
      llvm::outs() << L->name() << "\n";
    return 0;
  }

  if (!Executor) {
    llvm::errs() << llvm::toString(Executor.takeError()) << "\n";
    return 1;
  }

  bool BadLintName = false;
  auto checkLintNames = [&](const std::vector<std::string> &Names) {
    for (const auto &Name : Names) {
      if (llvm::any_of(AllLints,
                       [&](auto &L) { return L->name() == Name; }))
        continue;
      llvm::errs() << "unknown lint '" << Name << "'; available lints:";
      for (auto &L : AllLints)
        llvm::errs() << " " << L->name();
      llvm::errs() << "\n";
      BadLintName = true;
    }
  };
  checkLintNames(EnabledLintNames);
  checkLintNames(DisabledLintNames);
  if (BadLintName)
    return 1;

  std::vector<Lint *> Lints;
  for (auto &L : AllLints) {
    bool Enabled = EnabledLintNames.empty() ||
                   llvm::is_contained(EnabledLintNames, L->name());
    if (llvm::is_contained(DisabledLintNames, L->name()))
      Enabled = false;
    if (Enabled)
      Lints.push_back(L.get());
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

  auto Err = Executor->get()->execute(
      std::make_unique<LintFrontendActionFactory>(Lints));

  fflush(stderr);
  if (saved_stderr >= 0) {
    dup2(saved_stderr, STDERR_FILENO);
    close(saved_stderr);
  }

  if (Err) {
    llvm::errs() << llvm::toString(std::move(Err)) << "\n";
  }

  for (Lint *L : Lints)
    L->report();
}

python

sys.path.insert(0, '.')

from julia.printers import register_julia_printers
register_julia_printers(None)

end

# Quit without confirmation
define hook-quit
    set confirm off
end

set breakpoint pending on
break emit_function if ((int)strcmp(lam->name->name,"foobar")) == 0

run run.jl

# Skip 10 lines to uncompress the AST
next 10

print *ast
print *(jl_expr_t*)ast

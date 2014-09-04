################################################################################
# Configuration
#

import gdb

import re

import sys, traceback


################################################################################
# Auxiliary
#

visited = set()

void_ptr = gdb.lookup_type('void').pointer()

def get_typename(type):
    # If it points to a reference, get the reference.
    if type.code == gdb.TYPE_CODE_REF:
        type = type.target()

    # Get the unqualified type
    type = type.unqualified()

    return str(type)

def is_pointer(v):
    return (v.type.code == gdb.TYPE_CODE_PTR)

r_jl_type = re.compile("^_?jl_\w+_t$")

def is_julia_type(t):
    return (r_jl_type.match(get_typename(t)) != None)

def is_julia_pointer(v):
    return is_pointer(v) and is_julia_type(v.type.target())



################################################################################
# Printers
#

class CastingPrinter:
    def __init__(self, type_name, val):
        self.val = val
        self.type_name = type_name

        type_symbol, _ = gdb.lookup_symbol(type_name)
        if type_symbol is None:
            raise gdb.GdbError("Could not find type!")
        self.type = type_symbol.type

        self.casted_val = self.val.cast(self.type)

    def to_string(self):
        return self.type_name

    def children(self):
        # blacklist the current value its address
        # to avoid pointer recursion
        global visited
        self.pointer = long(self.val.address.cast(void_ptr))
        visited.add(self.pointer)

        for key in self.type.fields():
            val = self.casted_val[key.name]
            if is_pointer(val) and val.type != void_ptr:
                # if the field is a pointer, check whether it points to any parent
                # NOTE: void pointers will never be pretty-printed
                pointer = long(val.dereference().address.cast(void_ptr))
                if pointer == 0:
                    yield key.name, "0x0"
                elif pointer not in visited:
                    yield key.name, val
                elif pointer == self.pointer:
                    yield key.name, "<self>"
                else:
                    yield key.name, "<...>"
            else:
                # just stringify the plain value
                yield key.name, val

        visited.remove(self.pointer)

    def display_hint(self):
        return 'string'


class Decorator(object):
    def __init__(self, type_name, function):
        super(Decorator, self).__init__()
        self.type_name = type_name
        self.function = function
        self.enabled = True

    def invoke(self, value):
        if not self.enabled:
            return None
        return self.function(self.type_name, value)


# A pretty-printer that conforms to the "PrettyPrinter" protocol from
# gdb.printing.  It can also be used directly as an old-style printer.
class Printer(object):
    def __init__(self, name):
        super(Printer, self).__init__()
        self.name = name
        self.subprinters = []
        self.lookup = {}
        self.typevars = {}
        self.types = {}
        self.enabled = True

    def add(self, jl_typevar_name, jl_type_name, function):
        self.typevars[jl_typevar_name] = jl_type_name

        printer = Decorator(jl_type_name, function)
        self.subprinters.append(printer)
        self.lookup[jl_type_name] = printer

    def resolve_typevar_names(self):
        for jl_typevar_name, jl_type_name in self.typevars.iteritems():
            # Find the variable which points to our type, and is referred to
            # from the 'type' field in jl_value_t
            jl_typevar, _ = gdb.lookup_symbol(jl_typevar_name)
            if jl_typevar is None:
                raise gdb.GdbError("Could not find type variable!")
            jl_type_address = jl_typevar.value()

            self.types[long(jl_type_address)] = jl_type_name

    def __call__(self, val):
        typename = get_typename(val.type)
        if typename == None:
            return None

        # Dereference jl pointers
        if is_julia_pointer(val):
            val = val.dereference()
            typename = get_typename(val.type)
        # TODO: replace _jl_datatype_t with actual string type
        if not is_julia_type(val.type):
            return None
        if len(self.types) < 1:
            self.resolve_typevar_names()

        # Look-up the address in the type field
        try:
            typefield = val["type"]
        except:
            # Our jl_t doesn't have a type field...
            return None
        jl_type_address = typefield.dereference().address.cast(void_ptr)

        # Match the type field with a global type variable
        if long(jl_type_address) in self.types:
            # Find the actual type
            jl_type_name = self.types[long(jl_type_address)]

            # Expand it
            return self.lookup[jl_type_name].invoke(val)

        # Cannot find a pretty printer.  Return None.
        return None




################################################################################
# Initialization
#

# Try to use the new-style pretty-printing if available.
_use_gdb_pp = True
try:
    import gdb.printing
except ImportError:
    _use_gdb_pp = False

julia_printer = None

def register_julia_printers(obj):
    "Register Julia pretty-printers with objfile Obj."

    global _use_gdb_pp
    global julia_printer

    if _use_gdb_pp:
        gdb.printing.register_pretty_printer(obj, julia_printer)
    else:
        if obj is None:
            obj = gdb
        obj.pretty_printers.append(julia_printer)

def build_julia_typemap():
    global julia_printer

    julia_printer = Printer("julia")

    julia_printer.add("jl_expr_type", "jl_expr_t", CastingPrinter)
    julia_printer.add("jl_tuple_type", "jl_tuple_t", CastingPrinter)
    julia_printer.add("jl_datatype_type", "jl_datatype_t", CastingPrinter)

build_julia_typemap()

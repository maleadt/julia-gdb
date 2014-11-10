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
void_ptr = None

def get_typename(type):
    # If it points to a reference, get the reference.
    if type.code == gdb.TYPE_CODE_REF:
        type = type.target()

    # Get the unqualified type
    type = type.unqualified()

    return str(type)

def get_pointer_address(p):
    assert p.type.code == gdb.TYPE_CODE_PTR

    return long(p.cast(void_ptr))

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

blacklist = {
    '*': ['type', 'env'],
    'jl_datatype_t': ['instance', 'parameters', 'super']    # TODO: remove the jl_datatype_t blacklists when we resolve them to strings
}

# http://tromey.com/blog/?p=546

class CastingPrinter:
    def __init__(self, cast_type_name, print_type_name, val):
        self.val = val
        self.cast_type_name = cast_type_name
        self.print_type_name = print_type_name

        cast_type_symbol, _ = gdb.lookup_symbol(cast_type_name)
        if cast_type_symbol is None:
            raise gdb.GdbError("Could not find type!")
        self.cast_type = cast_type_symbol.type

        self.casted_val = self.val.cast(self.cast_type)

    def to_string(self):
        return self.print_type_name

    def children(self):
        # blacklist the current value its address
        # to avoid pointer recursion
        global visited
        self.pointer = get_pointer_address(self.val.address)
        visited.add(self.pointer)

        for key in self.cast_type.fields():
            # Manage blacklist
            if key.name in blacklist['*']:
                continue
            if self.print_type_name in blacklist and key.name in blacklist[self.print_type_name]:
                continue

            val = self.casted_val[key.name]
            if is_pointer(val) and val.type != void_ptr:
                # if the field is a pointer, check whether it points to any parent
                # NOTE: void pointers will never be pretty-printed
                pointer = get_pointer_address(val)
                # TODO: val.value()?
                # TODO: replace etype child with actual type?
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
    def __init__(self, cast_type_name, print_type_name, function):
        super(Decorator, self).__init__()
        self.cast_type_name = cast_type_name
        self.print_type_name = print_type_name
        self.function = function
        self.enabled = True

    def invoke(self, value):
        if not self.enabled:
            return None
        return self.function(self.cast_type_name, self.print_type_name, value)


# A pretty-printer that conforms to the "PrettyPrinter" protocol from
# gdb.printing.  It can also be used directly as an old-style printer.
class Printer(object):
    def __init__(self, name):
        super(Printer, self).__init__()
        self.name = name
        self.subprinters = []
        self.printers = {}
        self.typevars = {}
        self.types = {}
        self.enabled = True
        self.initialized = False
        self.enabled = True

    def __deferred__init__(self):
        # Do we even julia?
        try:
            gdb.lookup_type('jl_value_t')
        except:
            self.enabled = False
            return

        # Save the frequently-used 'void *' type, which is expensive to look-up
        global void_ptr
        void_ptr = gdb.lookup_type('void').pointer()

        # Resolve typename variables
        for typevar_name, jl_type_name in self.typevars.iteritems():
            # Find the variable which points to our type, and is referred to
            # from the 'type' field in jl_value_t
            jl_typevar, _ = gdb.lookup_symbol(typevar_name)
            if jl_typevar is None:
                raise gdb.GdbError("Could not find type variable!")
            jl_type_address = jl_typevar.value()

            self.types[long(jl_type_address)] = jl_type_name

        self.initialized = True

    def add(self, typevar_name, cast_type_name, print_type_name="", function=CastingPrinter):
        '''
            typevar_name: symbol where the type addresses point to
            cast_type_name: type which the jl_value_t should be cast to
            print_type_name: how we should name the type

            TODO: are there any types with different casting vs printing type?
        '''

        # If no distinct type name is provided for printing, use the casting one
        if print_type_name == "":
            print_type_name = cast_type_name

        self.typevars[typevar_name] = print_type_name

        printer = Decorator(cast_type_name, print_type_name, function)
        self.subprinters.append(printer)
        self.printers[print_type_name] = printer

    def resolve_julia_typename(self, val):
        # Check the type
        if not is_julia_type(val.type):
            return None

        # Look-up the address in the type field
        try:
            typefield = val["type"]
        except:
            # This jl_t doesn't have a type field (e.g. jl_fptr_t, ...)...
            return None
        jl_type_address = get_pointer_address(typefield)

        # Match the type field with a global type variable
        if jl_type_address in self.types:
            # Find the actual type
            jl_type_name = self.types[jl_type_address]
            return jl_type_name
        else:
            return None

    def __call__(self, val):
        if not self.enabled:
            return None

        # Deferred resolving of type addresses
        if not self.initialized:
            self.__deferred__init__()

        # Dereference pointers
        if is_julia_pointer(val):
            val = val.dereference()

        # Get the actual typename
        jl_type_name = self.resolve_julia_typename(val)
        if jl_type_name == None:
            return None

        # Expand it
        return self.printers[jl_type_name].invoke(val)




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

    julia_printer.add("jl_expr_type", "jl_expr_t")
    julia_printer.add("jl_tuple_type", "jl_tuple_t")

    julia_printer.add("jl_typename_type", "jl_typename_t")

    # Data types
    julia_printer.add("jl_datatype_type", "jl_datatype_t")
    julia_printer.add("jl_sym_type", "jl_sym_t")

build_julia_typemap()

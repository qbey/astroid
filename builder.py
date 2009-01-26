# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
"""The ASTNGBuilder makes astng from living object and / or from compiler.ast

With python >= 2.5, the internal _ast module is used instead

The builder is not thread safe and can't be used to parse different sources
at the same time.


:author:    Sylvain Thenault
:copyright: 2003-2008 LOGILAB S.A. (Paris, FRANCE)
:contact:   http://www.logilab.fr/ -- mailto:python-projects@logilab.org
:copyright: 2003-2008 Sylvain Thenault
:contact:   mailto:thenault@gmail.com
"""

__docformat__ = "restructuredtext en"

import sys
from os.path import splitext, basename, dirname, exists, abspath
from inspect import isfunction, ismethod, ismethoddescriptor, isclass, \
     isbuiltin
from inspect import isdatadescriptor

from logilab.common.fileutils import norm_read
from logilab.common.modutils import modpath_from_file

from logilab.astng import nodes
from logilab.astng._exceptions import ASTNGBuildingException, InferenceError
from logilab.astng.utils import ASTWalker
from logilab.astng.raw_building import *

try:
    from _ast import PyCF_ONLY_AST
    def parse(string):
        return compile(string, "<string>", 'exec', PyCF_ONLY_AST)
except:
    from compiler import parse
    from logilab.astng import patchcomptransformer
    
# ast NG builder ##############################################################

class ASTNGBuilder:
    """provide astng building methods
    """
    
    def __init__(self, manager=None):
        if manager is None:
            from logilab.astng import MANAGER as manager
        self._manager = manager
        self._module = None
        self._file = None
        self._done = None
        self._stack, self._par_stack = None, None
        self._metaclass = None        
        self._walker = ASTWalker(self)
        self._dyn_modname_map = {'gtk': 'gtk._gtk'}
        self._delayed = []
        
    def module_build(self, module, modname=None):
        """build an astng from a living module instance
        """
        node = None
        self._module = module
        path = getattr(module, '__file__', None)
        if path is not None:
            path_, ext = splitext(module.__file__)
            if ext in ('.py', '.pyc', '.pyo') and exists(path_ + '.py'):
                node = self.file_build(path_ + '.py', modname)
        if node is None:
            # this is a built-in module
            # get a partial representation by introspection
            node = self.inspect_build(module, modname=modname, path=path)
        return node

    def inspect_build(self, module, modname=None, path=None):
        """build astng from a living module (i.e. using inspect)
        this is used when there is no python source code available (either
        because it's a built-in module or because the .py is not available)
        """
        print '*'*80
        print 'inspect build', module, modname
        self._module = module
        node = build_module(modname or module.__name__, module.__doc__)
        node.file = node.path = path and abspath(path) or path
        if self._manager is not None:
            self._manager._cache[node.file] = self._manager._cache[node.name] = node
        node.package = hasattr(module, '__path__')
        attach___dict__(node)
        self._done = {}
        self.object_build(node, module)
        return node
    
    def file_build(self, path, modname=None):
        """build astng from a source code file (i.e. from an ast)

        path is expected to be a python source file
        """
        print '*'*80
        print 'file build', path, modname
        try:
            data = norm_read(path)
        except IOError, ex:
            msg = 'Unable to load file %r (%s)' % (path, ex)
            raise ASTNGBuildingException(msg)
        self._file = path
        # get module name if necessary, *before modifying sys.path*
        if modname is None:
            try:
                modname = '.'.join(modpath_from_file(path))
            except ImportError:
                modname = splitext(basename(path))[0]
        # build astng representation
        try:
            sys.path.insert(0, dirname(path))
            node = self.string_build(data, modname, path)
            node.file = abspath(path)
        finally:
            self._file = None
            sys.path.pop(0)
        
        return node
    
    def string_build(self, data, modname='', path=None):
        """build astng from a source code stream (i.e. from an ast)"""
        return self.ast_build(parse(data + '\n'), modname, path)
       
    def ast_build(self, node, modname='', path=None):
        """recurse on the ast (soon ng) to add some arguments et method"""
        print '*'*80
        print modname, path
        nodes.native_repr_tree(node)
        if path is not None:
            node.file = node.path = abspath(path)
        else:
            node.file = node.path = '<?>'
        if modname.endswith('.__init__'):
            modname = modname[:-9]
            node.package = True
        else:
            node.package = path and path.find('__init__.py') > -1 or False
        node.name = modname 
        node.pure_python = True
        self._asscontext = None
        if self._manager is not None:
            self._manager._cache[node.file] = node
            if self._file:
                self._manager._cache[abspath(self._file)] = node
        self._walker.walk(node)
        while self._delayed:
            dnode = self._delayed.pop(0)
            getattr(self, 'delayed_visit_%s' % dnode.__class__.__name__.lower())(dnode)
        return node

    # callbacks to build from an existing compiler.ast tree ###################
    
    def set_context(self, node, childnode):
        if isinstance(node, nodes.Assign):
            if childnode in node.targets:
                self._asscontext = node
            else:
                self._asscontext = None
        elif isinstance(node, (nodes.AugAssign, nodes.ListCompFor)):
            if childnode is node.target:
                self._asscontext = node
            else:
                self._asscontext = None
        
    def visit_default(self, node):
        """default visit method, handle the parent attribute"""
        try:
            node.parent = self._par_stack[-1]
        except:
            print node, self._par_stack[-1]
            raise
        #assert node.parent is not node
        self._par_stack.append(node)

    def leave_default(self, _):       
        """default leave method, handle the parent attribute"""
        self._par_stack.pop()             

    def visit_assign(self, node):
        """visit a Assign node -> check for classmethod and staticmethod
        + __metaclass__
        """
        self.visit_default(node)
        nodes.init_assign(node)
        klass = node.parent.frame()
        print node
        if isinstance(klass, nodes.Class) and \
            isinstance(node.value, nodes.CallFunc) and \
            isinstance(node.value.node, nodes.Name):
            func_name = node.value.node.name
            if func_name in ('classmethod', 'staticmethod'):
                for ass_node in node.nodes:
                    try:
                        meth = klass[ass_node.name]
                        if isinstance(meth, nodes.Function):
                            meth.type = func_name
                        #else:
                        #    print >> sys.stderr, 'FIXME 1', meth
                    except (AttributeError, KeyError):
                        #print >> sys.stderr, 'FIXME 2', ass_node.name
                        continue
        elif getattr(node.targets[0], 'name', None) == '__metaclass__': # XXX check more...
            self._metaclass[-1] = 'type' # XXX get the actual metaclass

    def visit_augassign(self, node):
        """visit a AugAssign node -> add name to locals"""
        self.visit_default(node)
        nodes.init_augassign(node)
        if not isinstance(node.target, nodes.Name):
            return  # XXX
        self._add_local(node, node.node.name)

    def visit_binop(self, node):
        self.visit_default(node)
        nodes.init_binop(node)

    def visit_boolop(self, node):
        self.visit_default(node)
        nodes.init_boolop(node)

    def visit_callfunc(self, node):
        self.visit_default(node)
        nodes.init_callfunc(node)

    def visit_compare(self, node):
        self.visit_default(node)
        nodes.init_compare(node)

    def visit_delete(self, node):
        self.visit_default(node)
        nodes.init_delete(node)

    def visit_discard(self, node):
        self.visit_default(node)
        nodes.init_discard(node)

    def visit_dict(self, node): 
        self.visit_default(node)
        nodes.init_dict(node)

    def visit_exec(self, node):
        self.visit_default(node)
        nodes.init_exec(node)

    def visit_getattr(self, node): 
        self.visit_default(node)
        nodes.init_getattr(node)

    def visit_import(self, node):
        """visit a Import node -> add imported names to locals"""
        self.visit_default(node)
        nodes.init_import(node)
        for (name, asname) in node.names:
            name = asname or name
            node.parent.set_local(name.split('.')[0], node)

    def visit_list(self, node): 
        self.visit_default(node)
        nodes.init_list(node)

    def visit_listcomp(self, node):
        self.visit_default(node)
        nodes.init_listcomp(node)

    def visit_listcompfor(self, node):
        self.visit_default(node)
        nodes.init_listcompfor(node)

    def visit_for(self, node):
        self.visit_default(node)
        nodes.init_for(node)
            
    def visit_from(self, node):
        """visit a From node -> add imported names to locals"""
        self.visit_default(node)
        nodes.init_import_from(node)
        # add names imported by the import to locals
        for (name, asname) in node.names:
            if name == '*':
                try:
                    imported = node.root().import_module(node.modname)
                except ASTNGBuildingException:
                    #import traceback
                    #traceback.print_exc()
                    continue
                    # FIXME: log error
                    #print >> sys.stderr, \
                    #      'Unable to get imported names for %r line %s"' % (
                    #    node.modname, node.lineno)
                for name in imported.wildcard_import_names():
                    node.parent.set_local(name, node)
            else:
                node.parent.set_local(asname or name, node)
                
    def visit_module(self, node):
        """visit a Module node -> init node and push the corresponding
        object or None on the top of the stack
        """
        self._stack = [self._module]
        self._par_stack = [node]
        self._metaclass = ['']
        self._global_names = []
        node.globals = node.locals = {}
        nodes.init_module(node)
        for name, value in ( ('__name__', node.name),
                             ('__file__', node.path),
                             ('__doc__', node.doc) ):
            const = nodes.const_factory(value)
            const.parent = node
            node.locals[name] = [const]
        if node.package:
            # FIXME: List(Const())
            const = nodes.const_factory(value)
            const.parent = node
            node.locals['__path__'] = [const]
        attach___dict__(node)

    def leave_module(self, _):
        """leave a Module node -> pop the last item on the stack and check
        the stack is empty
        """
        self._stack.pop()
        assert not self._stack, 'Stack is not empty : %s' % self._stack
        self._par_stack.pop()
        assert not self._par_stack, \
               'Parent stack is not empty : %s' % self._par_stack
                    
    def visit_name(self, node):
        nodes.init_name(node)
        try:
            cls, value = nodes.CONST_NAME_TRANSFORMS[node.name]
            node.__class__ = cls
            node.value = value
        except KeyError:
            pass
        if node.name == 'NoneType':
            print 'yoooooooooooo', node.name, self._asscontext
        self.visit_default(node)
        if self._asscontext is not None:
            self._add_local(node, node.name)
            
    def visit_print(self, node):
        self.visit_default(node)
        nodes.init_print(node)
        
    def visit_tryexcept(self, node):
        self.visit_default(node)
        nodes.init_try_except(node)

    def visit_tryfinally(self, node):
        self.visit_default(node)
        nodes.init_try_finally(node)

    def visit_tuple(self, node): 
        self.visit_default(node)
        nodes.init_tuple(node)

    def visit_unaryop(self, node):
        self.visit_default(node)
        nodes.init_unaryop(node)

    def visit_while(self, node):
        self.visit_default(node)
        nodes.init_while(node)




        
    def visit_class(self, node):
        """visit a Class node -> init node and push the corresponding
        object or None on the top of the stack
        """
        self.visit_default(node)
        node.instance_attrs = {}
        self._push(node)
        nodes.init_class(node)
        for name, value in ( ('__name__', node.name),
                             ('__module__', node.root().name),
                             ('__doc__', node.doc) ):
            const = nodes.const_factory(value)
            const.parent = node
            node.locals[name] = [const]
        attach___dict__(node)
        self._metaclass.append(self._metaclass[-1])
    visit_classdef = visit_class
        
    def leave_class(self, node):
        """leave a Class node -> pop the last item on the stack"""
        self.leave_default(node)
        self._stack.pop()
        metaclass = self._metaclass.pop()
        if not node.bases:
            # no base classes, detect new / style old style according to
            # current scope
            node._newstyle = metaclass == 'type'
        node.basenames = [bnode.as_string() for bnode in node.bases]
    leave_classdef = leave_class
        
    def visit_function(self, node):
        """visit a Function node -> init node and push the corresponding
        object or None on the top of the stack
        """
        self.visit_default(node)
        self._global_names.append({})
        nodes.init_function(node)
        if isinstance(node.parent.frame(), nodes.Class):
            if node.name == '__new__':
                node.type = 'classmethod'
            else:
                node.type = 'method'
        self._push(node)
        register_arguments(node, node.argnames)
    visit_functiondef = visit_function
    
    def leave_function(self, node):
        """leave a Function node -> pop the last item on the stack"""
        self.leave_default(node)
        self._stack.pop()
        self._global_names.pop()
    leave_functiondef = leave_function
        
    def visit_lambda(self, node):
        """visit a Lambda node -> init node locals"""
        self.visit_default(node)
        nodes.init_lambda(node)
        node.locals = {}
        register_arguments(node, node.argnames)
        
    def visit_genexpr(self, node):
        """visit a GenExpr node -> init node locals"""
        self.visit_default(node)
        node.locals = {}
    visit_generatorexp = visit_genexpr
    
    def visit_global(self, node):
        """visit a Global node -> add declared names to locals"""
        self.visit_default(node)
        if not self._global_names: # global at the module level, no effect
            return
        for name in node.names:
            self._global_names[-1].setdefault(name, []).append(node)
        
    def visit_subscript(self, node):
        self.visit_default(node)
        nodes.init_subscript(node)
        
    def _add_local(self, node, name):
        if self._global_names and name in self._global_names[-1]:
            node.root().set_local(name, node)
        else:
            node.parent.set_local(name, node)

    def _push(self, node):
        """update the stack and init some parts of the Function or Class node
        """
        obj = getattr(self._stack[-1], node.name, None)
        self._stack.append(obj)
        node.locals = {}
        node.parent.frame().set_local(node.name, node)
        
    # py2.4 (compiler mode) only callbacks ####################################

    def leave_decorators(self, node):
        """python >= 2.4
        visit a Decorator node -> check for classmethod and staticmethod
        """
        func = node.parent
        for decorator_expr in node.nodes:
            if isinstance(decorator_expr, nodes.Name) and \
                   decorator_expr.name in ('classmethod', 'staticmethod'):
                func.type = decorator_expr.name
        self.leave_default(node)
       
    def visit_assname(self, node):
        """visit a AssName node -> add name to locals"""
        self.visit_default(node)
        self._add_local(node, node.name)

    def visit_assattr(self, node):
        """visit a AssAttr node -> delay it to handle members
        definition later
        """
        self.visit_default(node)
        self._delayed.append(node)

    def visit_printnl(self, node):
        self.visit_default(node)
        nodes.init_printnl(node)
        
    def delayed_visit_assattr(self, node):
        """visit a AssAttr node -> add name to locals, handle members
        definition
        """
        #print 'delayted', node, node.attrname
        try:
            frame = node.frame()
            for infered in node.expr.infer():
                if infered is nodes.YES:
                    continue
                try:
                    if infered.__class__ is nodes.Instance:
                        infered = infered._proxied
                        iattrs = infered.instance_attrs
                    else:
                        iattrs = infered.locals
                except AttributeError:
                    import traceback
                    traceback.print_exc()
                    continue
                values = iattrs.setdefault(node.attrname, [])
                if node in values:
                    continue
                # get assign in __init__ first XXX useful ?
                if frame.name == '__init__' and values and not \
                       values[0].frame().name == '__init__':
                    values.insert(0, node)
                else:
                    values.append(node)
                #print node.attrname, infered, values
        except InferenceError:
            #print frame, node
            pass
        
    # py2.5 (ast mode) only callbacks #########################################

    def visit_attribute(self, node):
        """visit a Attribute node under a Assign -> delay it to handle
        members definition later
        """
        self.visit_default(node)
        if self._asscontext is not None:
            self._delayed.append(node)
    
    def visit_str(self, node):
        try:
            node.value = node.s
        except AttributeError:
            assert node.value
        self.visit_default(node)
        
    def visit_num(self, node):
        node.value = node.n
        self.visit_default(node)

    def delayed_visit_attribute(self, node):
        """visit a AssAttr node -> add name to locals, handle members definition
        """
        #print 'delayted', node, node.attrname
        try:
            frame = node.frame()
            for infered in node.value.infer():
                if infered is nodes.YES:
                    continue
                try:
                    if infered.__class__ is nodes.Instance:
                        infered = infered._proxied
                        iattrs = infered.instance_attrs
                    else:
                        iattrs = infered.locals
                except AttributeError:
                    import traceback
                    traceback.print_exc()
                    continue
                values = iattrs.setdefault(node.attr, [])
                if node in values:
                    continue
                # get assign in __init__ first XXX useful ?
                if frame.name == '__init__' and values and not \
                       values[0].frame().name == '__init__':
                    values.insert(0, node)
                else:
                    values.append(node)
                #print node.attrname, infered, values
        except InferenceError:
            #print frame, node
            pass

    # astng from living objects ###############################################
    #
    # this is actually a really minimal representation, including only Module,
    # Function and Class nodes and some others as guessed
    
    def object_build(self, node, obj):
        """recursive method which create a partial ast from real objects
         (only function, class, and method are handled)
        """
        if self._done.has_key(obj):
            return self._done[obj]
        self._done[obj] = node
        modname = self._module.__name__
        modfile = getattr(self._module, '__file__', None)
        for name in dir(obj):
            try:
                member = getattr(obj, name)
            except AttributeError:
                # damned ExtensionClass.Base, I know you're there !
                attach_dummy_node(node, name)
                continue
            if ismethod(member):
                member = member.im_func
            if isfunction(member):
                # verify this is not an imported function
                if member.func_code.co_filename != modfile:
                    attach_dummy_node(node, name, member)
                    continue
                object_build_function(node, member)
            elif isbuiltin(member):
                # verify this is not an imported member
                if self._member_module(member) != modname:
                    imported_member(node, member, name)
                    continue
                object_build_methoddescriptor(node, member)                
            elif isclass(member):
                # verify this is not an imported class
                if self._member_module(member) != modname:
                    imported_member(node, member, name)
                    continue
                if member in self._done:
                    class_node = self._done[member]
                    node.add_local_node(class_node, name)
                else:
                    class_node = object_build_class(node, member)
                # recursion
                self.object_build(class_node, member)
            elif ismethoddescriptor(member):
                assert isinstance(member, object)
                object_build_methoddescriptor(node, member)
            elif isdatadescriptor(member):
                assert isinstance(member, object)
                object_build_datadescriptor(node, member, name)
            elif isinstance(member, (int, long, float, str, unicode)) or member is None:
                attach_const_node(node, name, member)
            else:
                # create an empty node so that the name is actually defined
                attach_dummy_node(node, name, member)

    def _member_module(self, member):
        modname = getattr(member, '__module__', None)
        return self._dyn_modname_map.get(modname, modname)
        
def imported_member(node, member, name):
    """consider a class/builtin member where __module__ != current module name

    check if it's sound valid and then add an import node, else use a dummy node
    """
    # /!\ some classes like ExtensionClass doesn't have a 
    # __module__ attribute !
    member_module = getattr(member, '__module__', '__builtin__')
    try:
        getattr(sys.modules[member_module], name)
    except (KeyError, AttributeError):
        attach_dummy_node(node, name, member)
    else:
        attach_import_node(node, member_module, name)
    

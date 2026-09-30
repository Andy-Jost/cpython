// types.ChoiceType: the inert node that `lhs ? rhs` builds when neither
// operand handles the choice operator. Shape follows Objects/sliceobject.c;
// weakref and GC handling follow Objects/unionobject.c.

#include "Python.h"
#include "pycore_choiceobject.h"  // PyChoiceObject, _PyChoice_Type
#include "pycore_initconfig.h"    // _PyStatus_OK()
#include "pycore_modsupport.h"    // _PyArg_NoKeywords()
#include "pycore_object.h"        // _PyObject_GC_TRACK()
#include "pycore_pystate.h"       // _PyInterpreterState_GET()
#include "pycore_typeobject.h"    // _PyType_GetDict()
#include "pycore_weakref.h"       // FT_CLEAR_WEAKREFS()

#include <stddef.h>               // offsetof()


#define _PyChoice_CAST(op) _Py_CAST(PyChoiceObject*, (op))


PyObject *
_PyChoice_New(PyObject *lhs, PyObject *rhs)
{
    assert(lhs != NULL && rhs != NULL);
    PyChoiceObject *node = PyObject_GC_New(PyChoiceObject, &_PyChoice_Type);
    if (node == NULL) {
        return NULL;
    }
    node->lhs = Py_NewRef(lhs);
    node->rhs = Py_NewRef(rhs);
    node->weakreflist = NULL;
    _PyObject_GC_TRACK(node);
    return (PyObject *)node;
}

static PyObject *
choice_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    // Not Py_TPFLAGS_BASETYPE, so type is always &_PyChoice_Type.
    assert(type == &_PyChoice_Type);
    PyObject *lhs, *rhs;

    if (!_PyArg_NoKeywords("ChoiceType", kwds)) {
        return NULL;
    }
    if (!PyArg_UnpackTuple(args, "ChoiceType", 2, 2, &lhs, &rhs)) {
        return NULL;
    }
    return _PyChoice_New(lhs, rhs);
}

static void
choice_dealloc(PyObject *op)
{
    PyChoiceObject *node = _PyChoice_CAST(op);
    _PyObject_GC_UNTRACK(op);
    FT_CLEAR_WEAKREFS(op, node->weakreflist);
    // Py_XDECREF: choice_clear() may already have run during a GC pass.
    Py_XDECREF(node->lhs);
    Py_XDECREF(node->rhs);
    Py_TYPE(op)->tp_free(op);
}

static int
choice_traverse(PyObject *op, visitproc visit, void *arg)
{
    PyChoiceObject *node = _PyChoice_CAST(op);
    Py_VISIT(node->lhs);
    Py_VISIT(node->rhs);
    return 0;
}

static int
choice_clear(PyObject *op)
{
    PyChoiceObject *node = _PyChoice_CAST(op);
    Py_CLEAR(node->lhs);
    Py_CLEAR(node->rhs);
    return 0;
}

static PyObject *
choice_repr(PyObject *op)
{
    PyChoiceObject *node = _PyChoice_CAST(op);
    const char *name = _PyType_Name(Py_TYPE(op));   // "ChoiceType"

    int status = Py_ReprEnter(op);
    if (status != 0) {
        if (status > 0) {
            return PyUnicode_FromFormat("%s(...)", name);
        }
        return NULL;
    }
    PyObject *result = PyUnicode_FromFormat("%s(%R, %R)",
                                            name, node->lhs, node->rhs);
    Py_ReprLeave(op);
    return result;
}

static int
choice_bool(PyObject *Py_UNUSED(op))
{
    PyErr_SetString(PyExc_TypeError,
                    "the truth value of a ChoiceType is ambiguous");
    return -1;
}

static PyNumberMethods choice_as_number = {
    .nb_bool = choice_bool,
};

static PyObject *
choice_getnewargs(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    PyChoiceObject *node = _PyChoice_CAST(op);
    return PyTuple_Pack(2, node->lhs, node->rhs);
}

static PyMethodDef choice_methods[] = {
    {"__getnewargs__", choice_getnewargs, METH_NOARGS, NULL},
    {NULL, NULL}
};

static PyMemberDef choice_members[] = {
    {"lhs", _Py_T_OBJECT, offsetof(PyChoiceObject, lhs), Py_READONLY,
     PyDoc_STR("the left operand")},
    {"rhs", _Py_T_OBJECT, offsetof(PyChoiceObject, rhs), Py_READONLY,
     PyDoc_STR("the right operand")},
    {NULL}
};

// The first line is the __text_signature__ (see type_get_text_signature()
// in Objects/typeobject.c); test_inspect checks every name in types.__all__.
PyDoc_STRVAR(choice_doc,
"ChoiceType(lhs, rhs, /)\n"
"--\n\n"
"The type of a choice node.\n"
"\n"
"``lhs ? rhs`` evaluates to ChoiceType(lhs, rhs) when neither operand\n"
"handles the operator through __choice__ or __rchoice__. Nodes compare\n"
"and hash by identity and have no truth value.");

PyTypeObject _PyChoice_Type = {
    PyVarObject_HEAD_INIT(&PyType_Type, 0)
    .tp_name = "types.ChoiceType",
    .tp_basicsize = sizeof(PyChoiceObject),
    .tp_dealloc = choice_dealloc,
    .tp_repr = choice_repr,
    .tp_as_number = &choice_as_number,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_doc = choice_doc,
    .tp_traverse = choice_traverse,
    .tp_clear = choice_clear,
    // tp_hash and tp_richcompare stay NULL: inherit_slots() copies both
    // from object (identity semantics) when both are NULL.
    .tp_weaklistoffset = offsetof(PyChoiceObject, weakreflist),
    .tp_methods = choice_methods,
    .tp_members = choice_members,
    .tp_new = choice_new,
    .tp_free = PyObject_GC_Del,
};

PyStatus
_PyChoice_InitTypes(PyInterpreterState *interp)
{
    // Same recipe as initialize_structseq_dict() in Objects/structseq.c:
    // the type dict of a static builtin type is per-interpreter, so this
    // runs once per interpreter, after _PyTypes_InitTypes().
    assert(interp == _PyInterpreterState_GET());
    assert(_PyChoice_Type.tp_flags & Py_TPFLAGS_READY);
    PyObject *dict = _PyType_GetDict(&_PyChoice_Type);
    assert(dict != NULL);

    PyObject *match_args = Py_BuildValue("(ss)", "lhs", "rhs");
    if (match_args == NULL) {
        return _PyStatus_NO_MEMORY();
    }
    int rc = PyDict_SetItemString(dict, "__match_args__", match_args);
    Py_DECREF(match_args);
    if (rc < 0) {
        return _PyStatus_ERR("can't set types.ChoiceType.__match_args__");
    }
    return _PyStatus_OK();
}

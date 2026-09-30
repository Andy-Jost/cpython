// Choice node object interface (types.ChoiceType). See Objects/choiceobject.c.

#ifndef Py_INTERNAL_CHOICEOBJECT_H
#define Py_INTERNAL_CHOICEOBJECT_H
#ifdef __cplusplus
extern "C" {
#endif

#ifndef Py_BUILD_CORE
#  error "this header requires Py_BUILD_CORE define"
#endif

typedef struct {
    PyObject_HEAD
    PyObject *lhs;          // never NULL while the node is alive
    PyObject *rhs;          // never NULL while the node is alive
    PyObject *weakreflist;
} PyChoiceObject;

extern PyTypeObject _PyChoice_Type;

#define _PyChoice_Check(op) Py_IS_TYPE((op), &_PyChoice_Type)

// Build a node holding new references to both operands.
// Both arguments must be non-NULL. Used by PyNumber_Choice().
extern PyObject *_PyChoice_New(PyObject *lhs, PyObject *rhs);

// Set ChoiceType.__match_args__ in this interpreter's type dict.
// Must run after _PyTypes_InitTypes() has readied _PyChoice_Type.
extern PyStatus _PyChoice_InitTypes(PyInterpreterState *interp);

#ifdef __cplusplus
}
#endif
#endif  // !Py_INTERNAL_CHOICEOBJECT_H
